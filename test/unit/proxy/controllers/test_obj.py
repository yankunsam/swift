#!/usr/bin/env python
# Copyright (c) 2010-2012 OpenStack Foundation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import email.parser
import itertools
import math
import random
import time
import unittest
from collections import defaultdict
from contextlib import contextmanager
import json
from hashlib import md5

import mock
from eventlet import Timeout
from six import BytesIO
from six.moves import range

import swift
from swift.common import utils, swob, exceptions
from swift.common.exceptions import ChunkWriteTimeout
from swift.common.header_key_dict import HeaderKeyDict
from swift.common.utils import Timestamp
from swift.obj import server
from swift.proxy import server as proxy_server
from swift.proxy.controllers import obj
from swift.proxy.controllers.base import \
    get_container_info as _real_get_container_info
from swift.common.storage_policy import POLICIES, ECDriverError, StoragePolicy

from test.unit import FakeRing, FakeMemcache, fake_http_connect, \
    debug_logger, patch_policies, SlowBody, FakeStatus, \
    encode_frag_archive_bodies
from test.unit.proxy.test_server import node_error_count


def unchunk_body(chunked_body):
    body = ''
    remaining = chunked_body
    while remaining:
        hex_length, remaining = remaining.split('\r\n', 1)
        length = int(hex_length, 16)
        body += remaining[:length]
        remaining = remaining[length + 2:]
    return body


@contextmanager
def set_http_connect(*args, **kwargs):
    old_connect = swift.proxy.controllers.base.http_connect
    new_connect = fake_http_connect(*args, **kwargs)
    try:
        swift.proxy.controllers.base.http_connect = new_connect
        swift.proxy.controllers.obj.http_connect = new_connect
        swift.proxy.controllers.account.http_connect = new_connect
        swift.proxy.controllers.container.http_connect = new_connect
        yield new_connect
        left_over_status = list(new_connect.code_iter)
        if left_over_status:
            raise AssertionError('left over status %r' % left_over_status)
    finally:
        swift.proxy.controllers.base.http_connect = old_connect
        swift.proxy.controllers.obj.http_connect = old_connect
        swift.proxy.controllers.account.http_connect = old_connect
        swift.proxy.controllers.container.http_connect = old_connect


class PatchedObjControllerApp(proxy_server.Application):
    """
    This patch is just a hook over the proxy server's __call__ to ensure
    that calls to get_container_info will return the stubbed value for
    container_info if it's a container info call.
    """

    container_info = {}
    per_container_info = {}

    def __call__(self, *args, **kwargs):

        def _fake_get_container_info(env, app, swift_source=None):
            _vrs, account, container, _junk = utils.split_path(
                env['PATH_INFO'], 3, 4)

            # Seed the cache with our container info so that the real
            # get_container_info finds it.
            ic = env.setdefault('swift.infocache', {})
            cache_key = "container/%s/%s" % (account, container)

            old_value = ic.get(cache_key)

            # Copy the container info so we don't hand out a reference to a
            # mutable thing that's set up only once at compile time. Nothing
            # *should* mutate it, but it's better to be paranoid than wrong.
            if container in self.per_container_info:
                ic[cache_key] = self.per_container_info[container].copy()
            else:
                ic[cache_key] = self.container_info.copy()

            real_info = _real_get_container_info(env, app, swift_source)

            if old_value is None:
                del ic[cache_key]
            else:
                ic[cache_key] = old_value

            return real_info

        with mock.patch('swift.proxy.server.get_container_info',
                        new=_fake_get_container_info), \
                mock.patch('swift.proxy.controllers.base.get_container_info',
                           new=_fake_get_container_info):
            return super(
                PatchedObjControllerApp, self).__call__(*args, **kwargs)


def make_footers_callback(body=None):
    # helper method to create a footers callback that will generate some fake
    # footer metadata
    cont_etag = 'container update etag may differ'
    crypto_etag = '20242af0cd21dd7195a10483eb7472c9'
    etag_crypto_meta = \
        '{"cipher": "AES_CTR_256", "iv": "sD+PSw/DfqYwpsVGSo0GEw=="}'
    etag = md5(body).hexdigest() if body is not None else None
    footers_to_add = {
        'X-Object-Sysmeta-Container-Update-Override-Etag': cont_etag,
        'X-Object-Sysmeta-Crypto-Etag': crypto_etag,
        'X-Object-Sysmeta-Crypto-Meta-Etag': etag_crypto_meta,
        'X-I-Feel-Lucky': 'Not blocked',
        'Etag': etag}

    def footers_callback(footers):
        footers.update(footers_to_add)

    return footers_callback


class BaseObjectControllerMixin(object):
    container_info = {
        'status': 200,
        'write_acl': None,
        'read_acl': None,
        'storage_policy': None,
        'sync_key': None,
        'versions': None,
    }

    # this needs to be set on the test case
    controller_cls = None

    def setUp(self):
        # setup fake rings with handoffs
        for policy in POLICIES:
            policy.object_ring.max_more_nodes = policy.object_ring.replicas

        self.logger = debug_logger('proxy-server')
        self.logger.thread_locals = ('txn1', '127.0.0.2')
        self.app = PatchedObjControllerApp(
            None, FakeMemcache(), account_ring=FakeRing(),
            container_ring=FakeRing(), logger=self.logger)

        # you can over-ride the container_info just by setting it on the app
        # (see PatchedObjControllerApp for details)
        self.app.container_info = dict(self.container_info)

        # default policy and ring references
        self.policy = POLICIES.default
        self.obj_ring = self.policy.object_ring
        self._ts_iter = (utils.Timestamp(t) for t in
                         itertools.count(int(time.time())))

    def ts(self):
        return next(self._ts_iter)

    def replicas(self, policy=None):
        policy = policy or POLICIES.default
        return policy.object_ring.replicas

    def quorum(self, policy=None):
        policy = policy or POLICIES.default
        return policy.quorum

    def test_iter_nodes_local_first_noops_when_no_affinity(self):
        # this test needs a stable node order - most don't
        self.app.sort_nodes = lambda l: l
        controller = self.controller_cls(
            self.app, 'a', 'c', 'o')
        self.app.write_affinity_is_local_fn = None
        object_ring = self.app.get_object_ring(None)
        all_nodes = object_ring.get_part_nodes(1)
        all_nodes.extend(object_ring.get_more_nodes(1))

        local_first_nodes = list(controller.iter_nodes_local_first(
            object_ring, 1))

        self.maxDiff = None

        self.assertEqual(all_nodes, local_first_nodes)

    def test_iter_nodes_local_first_moves_locals_first(self):
        controller = self.controller_cls(
            self.app, 'a', 'c', 'o')
        self.app.write_affinity_is_local_fn = (
            lambda node: node['region'] == 1)
        # we'll write to one more than replica count local nodes
        self.app.write_affinity_node_count = lambda r: r + 1

        object_ring = self.app.get_object_ring(None)
        # make our fake ring have plenty of nodes, and not get limited
        # artificially by the proxy max request node count
        object_ring.max_more_nodes = 100000
        # nothing magic about * 2 + 3, just a way to make it bigger
        self.app.request_node_count = lambda r: r * 2 + 3

        all_nodes = object_ring.get_part_nodes(1)
        all_nodes.extend(object_ring.get_more_nodes(1))

        # limit to the number we're going to look at in this request
        nodes_requested = self.app.request_node_count(object_ring.replicas)
        all_nodes = all_nodes[:nodes_requested]

        # make sure we have enough local nodes (sanity)
        all_local_nodes = [n for n in all_nodes if
                           self.app.write_affinity_is_local_fn(n)]
        self.assertGreaterEqual(len(all_local_nodes), self.replicas() + 1)

        # finally, create the local_first_nodes iter and flatten it out
        local_first_nodes = list(controller.iter_nodes_local_first(
            object_ring, 1))

        # the local nodes move up in the ordering
        self.assertEqual([1] * (self.replicas() + 1), [
            node['region'] for node in local_first_nodes[
                :self.replicas() + 1]])
        # we don't skip any nodes
        self.assertEqual(len(all_nodes), len(local_first_nodes))
        self.assertEqual(sorted(all_nodes), sorted(local_first_nodes))

    def test_iter_nodes_local_first_best_effort(self):
        controller = self.controller_cls(
            self.app, 'a', 'c', 'o')
        self.app.write_affinity_is_local_fn = (
            lambda node: node['region'] == 1)

        object_ring = self.app.get_object_ring(None)
        all_nodes = object_ring.get_part_nodes(1)
        all_nodes.extend(object_ring.get_more_nodes(1))

        local_first_nodes = list(controller.iter_nodes_local_first(
            object_ring, 1))

        # we won't have quite enough local nodes...
        self.assertEqual(len(all_nodes), self.replicas() +
                         POLICIES.default.object_ring.max_more_nodes)
        all_local_nodes = [n for n in all_nodes if
                           self.app.write_affinity_is_local_fn(n)]
        self.assertEqual(len(all_local_nodes), self.replicas())
        # but the local nodes we do have are at the front of the local iter
        first_n_local_first_nodes = local_first_nodes[:len(all_local_nodes)]
        self.assertEqual(sorted(all_local_nodes),
                         sorted(first_n_local_first_nodes))
        # but we *still* don't *skip* any nodes
        self.assertEqual(len(all_nodes), len(local_first_nodes))
        self.assertEqual(sorted(all_nodes), sorted(local_first_nodes))

    def test_connect_put_node_timeout(self):
        controller = self.controller_cls(
            self.app, 'a', 'c', 'o')
        req = swift.common.swob.Request.blank('/v1/a/c/o')
        self.app.conn_timeout = 0.05
        with set_http_connect(slow_connect=True):
            nodes = [dict(ip='', port='', device='')]
            res = controller._connect_put_node(nodes, '', req, {}, ('', ''))
        self.assertIsNone(res)

    def test_DELETE_simple(self):
        req = swift.common.swob.Request.blank('/v1/a/c/o', method='DELETE')
        codes = [204] * self.replicas()
        with set_http_connect(*codes):
            resp = req.get_response(self.app)
        self.assertEqual(resp.status_int, 204)

    def test_DELETE_missing_one(self):
        # Obviously this test doesn't work if we're testing 1 replica.
        # In that case, we don't have any failovers to check.
        if self.replicas() == 1:
            return
        req = swift.common.swob.Request.blank('/v1/a/c/o', method='DELETE')
        codes = [404] + [204] * (self.replicas() - 1)
        random.shuffle(codes)
        with set_http_connect(*codes):
            resp = req.get_response(self.app)
        self.assertEqual(resp.status_int, 204)

    def test_DELETE_not_found(self):
        # Obviously this test doesn't work if we're testing 1 replica.
        # In that case, we don't have any failovers to check.
        if self.replicas() == 1:
            return
        req = swift.common.swob.Request.blank('/v1/a/c/o', method='DELETE')
        codes = [404] * (self.replicas() - 1) + [204]
        with set_http_connect(*codes):
            resp = req.get_response(self.app)
        self.assertEqual(resp.status_int, 404)

    def test_DELETE_mostly_found(self):
        req = swift.common.swob.Request.blank('/v1/a/c/o', method='DELETE')
        mostly_204s = [204] * self.quorum()
        codes = mostly_204s + [404] * (self.replicas() - len(mostly_204s))
        self.assertEqual(len(codes), self.replicas())
        with set_http_connect(*codes):
            resp = req.get_response(self.app)
        self.assertEqual(resp.status_int, 204)

    def test_DELETE_mostly_not_found(self):
        req = swift.common.swob.Request.blank('/v1/a/c/o', method='DELETE')
        mostly_404s = [404] * self.quorum()
        codes = mostly_404s + [204] * (self.replicas() - len(mostly_404s))
        self.assertEqual(len(codes), self.replicas())
        with set_http_connect(*codes):
            resp = req.get_response(self.app)
        self.assertEqual(resp.status_int, 404)

    def test_DELETE_half_not_found_statuses(self):
        self.obj_ring.set_replicas(4)

        req = swift.common.swob.Request.blank('/v1/a/c/o', method='DELETE')
        with set_http_connect(404, 204, 404, 204):
            resp = req.get_response(self.app)
        self.assertEqual(resp.status_int, 204)

    def test_DELETE_half_not_found_headers_and_body(self):
        # Transformed responses have bogus bodies and headers, so make sure we
        # send the client headers and body from a real node's response.
        self.obj_ring.set_replicas(4)

        status_codes = (404, 404, 204, 204)
        bodies = ('not found', 'not found', '', '')
        headers = [{}, {}, {'Pick-Me': 'yes'}, {'Pick-Me': 'yes'}]

        req = swift.common.swob.Request.blank('/v1/a/c/o', method='DELETE')
        with set_http_connect(*status_codes, body_iter=bodies,
                              headers=headers):
            resp = req.get_response(self.app)
        self.assertEqual(resp.status_int, 204)
        self.assertEqual(resp.headers.get('Pick-Me'), 'yes')
        self.assertEqual(resp.body, '')

    def test_DELETE_handoff(self):
        req = swift.common.swob.Request.blank('/v1/a/c/o', method='DELETE')
        codes = [204] * self.replicas()
        with set_http_connect(507, *codes):
            resp = req.get_response(self.app)
        self.assertEqual(resp.status_int, 204)

    def test_POST_non_int_delete_after(self):
        t = str(int(time.time() + 100)) + '.1'
        req = swob.Request.blank('/v1/a/c/o', method='POST',
                                 headers={'Content-Type': 'foo/bar',
                                          'X-Delete-After': t})
        resp = req.get_response(self.app)
        self.assertEqual(resp.status_int, 400)
        self.assertEqual('Non-integer X-Delete-After', resp.body)

    def test_PUT_non_int_delete_after(self):
        t = str(int(time.time() + 100)) + '.1'
        req = swob.Request.blank('/v1/a/c/o', method='PUT', body='',
                                 headers={'Content-Type': 'foo/bar',
                                          'X-Delete-After': t})
        with set_http_connect():
            resp = req.get_response(self.app)
        self.assertEqual(resp.status_int, 400)
        self.assertEqual('Non-integer X-Delete-After', resp.body)

    def test_POST_negative_delete_after(self):
        req = swob.Request.blank('/v1/a/c/o', method='POST',
                                 headers={'Content-Type': 'foo/bar',
                                          'X-Delete-After': '-60'})
        resp = req.get_response(self.app)
        self.assertEqual(resp.status_int, 400)
        self.assertEqual('X-Delete-After in past', resp.body)

    def test_PUT_negative_delete_after(self):
        req = swob.Request.blank('/v1/a/c/o', method='PUT', body='',
                                 headers={'Content-Type': 'foo/bar',
                                          'X-Delete-After': '-60'})
        with set_http_connect():
            resp = req.get_response(self.app)
        self.assertEqual(resp.status_int, 400)
        self.assertEqual('X-Delete-After in past', resp.body)

    def test_POST_delete_at_non_integer(self):
        t = str(int(time.time() + 100)) + '.1'
        req = swob.Request.blank('/v1/a/c/o', method='POST',
                                 headers={'Content-Type': 'foo/bar',
                                          'X-Delete-At': t})
        resp = req.get_response(self.app)
        self.assertEqual(resp.status_int, 400)
        self.assertEqual('Non-integer X-Delete-At', resp.body)

    def test_PUT_delete_at_non_integer(self):
        t = str(int(time.time() - 100)) + '.1'
        req = swob.Request.blank('/v1/a/c/o', method='PUT', body='',
                                 headers={'Content-Type': 'foo/bar',
                                          'X-Delete-At': t})
        with set_http_connect():
            resp = req.get_response(self.app)
        self.assertEqual(resp.status_int, 400)
        self.assertEqual('Non-integer X-Delete-At', resp.body)

    def test_POST_delete_at_in_past(self):
        t = str(int(time.time() - 100))
        req = swob.Request.blank('/v1/a/c/o', method='POST',
                                 headers={'Content-Type': 'foo/bar',
                                          'X-Delete-At': t})
        resp = req.get_response(self.app)
        self.assertEqual(resp.status_int, 400)
        self.assertEqual('X-Delete-At in past', resp.body)

    def test_PUT_delete_at_in_past(self):
        t = str(int(time.time() - 100))
        req = swob.Request.blank('/v1/a/c/o', method='PUT', body='',
                                 headers={'Content-Type': 'foo/bar',
                                          'X-Delete-At': t})
        with set_http_connect():
            resp = req.get_response(self.app)
        self.assertEqual(resp.status_int, 400)
        self.assertEqual('X-Delete-At in past', resp.body)

    def test_HEAD_simple(self):
        req = swift.common.swob.Request.blank('/v1/a/c/o', method='HEAD')
        with set_http_connect(200):
            resp = req.get_response(self.app)
        self.assertEqual(resp.status_int, 200)
        self.assertIn('Accept-Ranges', resp.headers)

    def test_HEAD_x_newest(self):
        req = swift.common.swob.Request.blank('/v1/a/c/o', method='HEAD',
                                              headers={'X-Newest': 'true'})
        with set_http_connect(*([200] * self.replicas())):
            resp = req.get_response(self.app)
        self.assertEqual(resp.status_int, 200)

    def test_HEAD_x_newest_different_timestamps(self):
        req = swob.Request.blank('/v1/a/c/o', method='HEAD',
                                 headers={'X-Newest': 'true'})
        ts = (utils.Timestamp(t) for t in itertools.count(int(time.time())))
        timestamps = [next(ts) for i in range(self.replicas())]
        newest_timestamp = timestamps[-1]
        random.shuffle(timestamps)
        backend_response_headers = [{
            'X-Backend-Timestamp': t.internal,
            'X-Timestamp': t.normal
        } for t in timestamps]
        with set_http_connect(*([200] * self.replicas()),
                              headers=backend_response_headers):
            resp = req.get_response(self.app)
        self.assertEqual(resp.status_int, 200)
        self.assertEqual(resp.headers['x-timestamp'], newest_timestamp.normal)

    def test_HEAD_x_newest_with_two_vector_timestamps(self):
        req = swob.Request.blank('/v1/a/c/o', method='HEAD',
                                 headers={'X-Newest': 'true'})
        ts = (utils.Timestamp(time.time(), offset=offset)
              for offset in itertools.count())
        timestamps = [next(ts) for i in range(self.replicas())]
        newest_timestamp = timestamps[-1]
        random.shuffle(timestamps)
        backend_response_headers = [{
            'X-Backend-Timestamp': t.internal,
            'X-Timestamp': t.normal
        } for t in timestamps]
        with set_http_connect(*([200] * self.replicas()),
                              headers=backend_response_headers):
            resp = req.get_response(self.app)
        self.assertEqual(resp.status_int, 200)
        self.assertEqual(resp.headers['x-backend-timestamp'],
                         newest_timestamp.internal)

    def test_HEAD_x_newest_with_some_missing(self):
        req = swob.Request.blank('/v1/a/c/o', method='HEAD',
                                 headers={'X-Newest': 'true'})
        ts = (utils.Timestamp(t) for t in itertools.count(int(time.time())))
        request_count = self.app.request_node_count(self.obj_ring.replicas)
        backend_response_headers = [{
            'x-timestamp': next(ts).normal,
        } for i in range(request_count)]
        responses = [404] * (request_count - 1)
        responses.append(200)
        request_log = []

        def capture_requests(ip, port, device, part, method, path,
                             headers=None, **kwargs):
            req = {
                'ip': ip,
                'port': port,
                'device': device,
                'part': part,
                'method': method,
                'path': path,
                'headers': headers,
            }
            request_log.append(req)
        with set_http_connect(*responses,
                              headers=backend_response_headers,
                              give_connect=capture_requests):
            resp = req.get_response(self.app)
        self.assertEqual(resp.status_int, 200)
        for req in request_log:
            self.assertEqual(req['method'], 'HEAD')
            self.assertEqual(req['path'], '/a/c/o')

    def test_container_sync_delete(self):
        ts = (utils.Timestamp(t) for t in itertools.count(int(time.time())))
        test_indexes = [None] + [int(p) for p in POLICIES]
        for policy_index in test_indexes:
            req = swob.Request.blank(
                '/v1/a/c/o', method='DELETE', headers={
                    'X-Timestamp': next(ts).internal})
            codes = [409] * self.obj_ring.replicas
            ts_iter = itertools.repeat(next(ts).internal)
            with set_http_connect(*codes, timestamps=ts_iter):
                resp = req.get_response(self.app)
            self.assertEqual(resp.status_int, 409)

    def test_PUT_requires_length(self):
        req = swift.common.swob.Request.blank('/v1/a/c/o', method='PUT')
        resp = req.get_response(self.app)
        self.assertEqual(resp.status_int, 411)

    def test_container_update_backend_requests(self):
        for policy in POLICIES:
            req = swift.common.swob.Request.blank(
                '/v1/a/c/o', method='PUT',
                headers={'Content-Length': '0',
                         'X-Backend-Storage-Policy-Index': int(policy)})
            controller = self.controller_cls(self.app, 'a', 'c', 'o')

            # This is the number of container updates we're doing, simulating
            # 1 to 15 container replicas.
            for num_containers in range(1, 16):
                containers = [{'ip': '1.0.0.%s' % i,
                               'port': '60%s' % str(i).zfill(2),
                               'device': 'sdb'} for i in range(num_containers)]

                backend_headers = controller._backend_requests(
                    req, self.replicas(policy), 1, containers)

                # how many of the backend headers have a container update
                container_updates = len(
                    [headers for headers in backend_headers
                     if 'X-Container-Partition' in headers])

                if num_containers <= self.quorum(policy):
                    # filling case
                    expected = min(self.quorum(policy) + 1,
                                   self.replicas(policy))
                else:
                    # container updates >= object replicas
                    expected = min(num_containers,
                                   self.replicas(policy))

                self.assertEqual(container_updates, expected)

# end of BaseObjectControllerMixin


@patch_policies()
class TestReplicatedObjController(BaseObjectControllerMixin,
                                  unittest.TestCase):

    controller_cls = obj.ReplicatedObjectController

    def test_PUT_simple(self):
        req = swift.common.swob.Request.blank('/v1/a/c/o', method='PUT')
        req.headers['content-length'] = '0'
        with set_http_connect(201, 201, 201):
            resp = req.get_response(self.app)
        self.assertEqual(resp.status_int, 201)

    def test_PUT_error_with_footers(self):
        footers_callback = make_footers_callback('')
        env = {'swift.callback.update_footers': footers_callback}
        req = swift.common.swob.Request.blank('/v1/a/c/o', method='PUT',
                                              environ=env)
        req.headers['content-length'] = '0'
        codes = [503] * self.replicas()
        expect_headers = {
            'X-Obj-Metadata-Footer': 'yes'
        }

        with set_http_connect(*codes, expect_headers=expect_headers):
            resp = req.get_response(self.app)
        self.assertEqual(resp.status_int, 503)

    def _test_PUT_with_no_footers(self, test_body='', chunked=False):
        # verify that when no footers are required then the PUT uses a regular
        # single part body
        req = swift.common.swob.Request.blank('/v1/a/c/o', method='PUT',
                                              body=test_body)
        if chunked:
            req.headers['Transfer-Encoding'] = 'chunked'
        etag = md5(test_body).hexdigest()
        req.headers['Etag'] = etag

        put_requests = defaultdict(
            lambda: {'headers': None, 'chunks': [], 'connection': None})

        def capture_body(conn, chunk):
            put_requests[conn.connection_id]['chunks'].append(chunk)
            put_requests[conn.connection_id]['connection'] = conn

        def capture_headers(ip, port, device, part, method, path, headers,
                            **kwargs):
            conn_id = kwargs['connection_id']
            put_requests[conn_id]['headers'] = headers

        codes = [201] * self.replicas()
        expect_headers = {'X-Obj-Metadata-Footer': 'yes'}
        resp_headers = {
            'Some-Header': 'Four',
            'Etag': '"%s"' % etag,
        }
        with set_http_connect(*codes, expect_headers=expect_headers,
                              give_send=capture_body,
                              give_connect=capture_headers,
                              headers=resp_headers):
            resp = req.get_response(self.app)

        self.assertEqual(resp.status_int, 201)
        timestamps = {captured_req['headers']['x-timestamp']
                      for captured_req in put_requests.values()}
        self.assertEqual(1, len(timestamps), timestamps)
        self.assertEqual(dict(resp.headers), {
            'Content-Type': 'text/html; charset=UTF-8',
            'Content-Length': '0',
            'Etag': etag,
            'Last-Modified': time.strftime(
                "%a, %d %b %Y %H:%M:%S GMT",
                time.gmtime(math.ceil(float(timestamps.pop())))),
        })
        for connection_id, info in put_requests.items():
            body = ''.join(info['chunks'])
            headers = info['headers']
            if chunked:
                body = unchunk_body(body)
                self.assertEqual('100-continue', headers['Expect'])
                self.assertEqual('chunked', headers['Transfer-Encoding'])
            else:
                self.assertNotIn('Transfer-Encoding', headers)
            if body:
                self.assertEqual('100-continue', headers['Expect'])
            else:
                self.assertNotIn('Expect', headers)
            self.assertNotIn('X-Backend-Obj-Multipart-Mime-Boundary', headers)
            self.assertNotIn('X-Backend-Obj-Metadata-Footer', headers)
            self.assertNotIn('X-Backend-Obj-Multiphase-Commit', headers)
            self.assertEqual(etag, headers['Etag'])

            self.assertEqual(test_body, body)
            self.assertTrue(info['connection'].closed)

    def test_PUT_with_chunked_body_and_no_footers(self):
        self._test_PUT_with_no_footers(test_body='asdf', chunked=True)

    def test_PUT_with_body_and_no_footers(self):
        self._test_PUT_with_no_footers(test_body='asdf', chunked=False)

    def test_PUT_with_no_body_and_no_footers(self):
        self._test_PUT_with_no_footers(test_body='', chunked=False)

    def _test_PUT_with_footers(self, test_body=''):
        # verify that when footers are required the PUT body is multipart
        # and the footers are appended
        footers_callback = make_footers_callback(test_body)
        env = {'swift.callback.update_footers': footers_callback}
        req = swift.common.swob.Request.blank('/v1/a/c/o', method='PUT',
                                              environ=env)
        req.body = test_body
        # send bogus Etag header to differentiate from footer value
        req.headers['Etag'] = 'header_etag'
        codes = [201] * self.replicas()
        expect_headers = {
            'X-Obj-Metadata-Footer': 'yes'
        }

        put_requests = defaultdict(
            lambda: {'headers': None, 'chunks': [], 'connection': None})

        def capture_body(conn, chunk):
            put_requests[conn.connection_id]['chunks'].append(chunk)
            put_requests[conn.connection_id]['connection'] = conn

        def capture_headers(ip, port, device, part, method, path, headers,
                            **kwargs):
            conn_id = kwargs['connection_id']
            put_requests[conn_id]['headers'] = headers

        resp_headers = {
            'Etag': '"resp_etag"',
            # NB: ignored!
            'Some-Header': 'Four',
        }
        with set_http_connect(*codes, expect_headers=expect_headers,
                              give_send=capture_body,
                              give_connect=capture_headers,
                              headers=resp_headers):
            resp = req.get_response(self.app)

        self.assertEqual(resp.status_int, 201)
        timestamps = {captured_req['headers']['x-timestamp']
                      for captured_req in put_requests.values()}
        self.assertEqual(1, len(timestamps), timestamps)
        self.assertEqual(dict(resp.headers), {
            'Content-Type': 'text/html; charset=UTF-8',
            'Content-Length': '0',
            'Etag': 'resp_etag',
            'Last-Modified': time.strftime(
                "%a, %d %b %Y %H:%M:%S GMT",
                time.gmtime(math.ceil(float(timestamps.pop())))),
        })
        for connection_id, info in put_requests.items():
            body = unchunk_body(''.join(info['chunks']))
            headers = info['headers']
            boundary = headers['X-Backend-Obj-Multipart-Mime-Boundary']
            self.assertTrue(boundary is not None,
                            "didn't get boundary for conn %r" % (
                                connection_id,))
            self.assertEqual('chunked', headers['Transfer-Encoding'])
            self.assertEqual('100-continue', headers['Expect'])
            self.assertEqual('yes', headers['X-Backend-Obj-Metadata-Footer'])
            self.assertNotIn('X-Backend-Obj-Multiphase-Commit', headers)
            self.assertEqual('header_etag', headers['Etag'])

            # email.parser.FeedParser doesn't know how to take a multipart
            # message and boundary together and parse it; it only knows how
            # to take a string, parse the headers, and figure out the
            # boundary on its own.
            parser = email.parser.FeedParser()
            parser.feed(
                "Content-Type: multipart/nobodycares; boundary=%s\r\n\r\n" %
                boundary)
            parser.feed(body)
            message = parser.close()

            self.assertTrue(message.is_multipart())  # sanity check
            mime_parts = message.get_payload()
            # notice, no commit confirmation
            self.assertEqual(len(mime_parts), 2)
            obj_part, footer_part = mime_parts

            self.assertEqual(obj_part['X-Document'], 'object body')
            self.assertEqual(test_body, obj_part.get_payload())

            # validate footer metadata
            self.assertEqual(footer_part['X-Document'], 'object metadata')
            footer_metadata = json.loads(footer_part.get_payload())
            self.assertTrue(footer_metadata)
            expected = {}
            footers_callback(expected)
            self.assertDictEqual(expected, footer_metadata)

            self.assertTrue(info['connection'].closed)

    def test_PUT_with_body_and_footers(self):
        self._test_PUT_with_footers(test_body='asdf')

    def test_PUT_with_no_body_and_footers(self):
        self._test_PUT_with_footers()

    def test_txn_id_logging_on_PUT(self):
        req = swift.common.swob.Request.blank('/v1/a/c/o', method='PUT')
        self.app.logger.txn_id = req.environ['swift.trans_id'] = 'test-txn-id'
        req.headers['content-length'] = '0'
        # we capture stdout since the debug log formatter prints the formatted
        # message to stdout
        stdout = BytesIO()
        with set_http_connect((100, Timeout()), 503, 503), \
                mock.patch('sys.stdout', stdout):
            resp = req.get_response(self.app)
        self.assertEqual(resp.status_int, 503)
        for line in stdout.getvalue().splitlines():
            self.assertIn('test-txn-id', line)
        self.assertIn('Trying to get final status of PUT to',
                      stdout.getvalue())

    def test_PUT_empty_bad_etag(self):
        req = swift.common.swob.Request.blank('/v1/a/c/o', method='PUT')
        req.headers['Content-Length'] = '0'
        req.headers['Etag'] = '"catbus"'

        # The 2-tuple here makes getexpect() return 422, not 100. For objects
        # that are >0 bytes, you get a 100 Continue and then a 422
        # Unprocessable Entity after sending the body. For zero-byte objects,
        # though, you get the 422 right away because no Expect header is sent
        # with zero-byte PUT. The second status in the tuple should not be
        # consumed, it's just there to make the FakeStatus treat the first as
        # an expect status, but we'll make it something other than a 422 so
        # that if it is consumed then the test should fail.
        codes = [FakeStatus((422, 200))
                 for _junk in range(self.replicas())]

        with set_http_connect(*codes):
            resp = req.get_response(self.app)
        self.assertEqual(resp.status_int, 422)

    def test_PUT_if_none_match(self):
        req = swift.common.swob.Request.blank('/v1/a/c/o', method='PUT')
        req.headers['if-none-match'] = '*'
        req.headers['content-length'] = '0'
        with set_http_connect(201, 201, 201):
            resp = req.get_response(self.app)
        self.assertEqual(resp.status_int, 201)

    def test_PUT_if_none_match_denied(self):
        req = swift.common.swob.Request.blank('/v1/a/c/o', method='PUT')
        req.headers['if-none-match'] = '*'
        req.headers['content-length'] = '0'
        with set_http_connect(201, 412, 201):
            resp = req.get_response(self.app)
        self.assertEqual(resp.status_int, 412)

    def test_PUT_if_none_match_not_star(self):
        req = swift.common.swob.Request.blank('/v1/a/c/o', method='PUT')
        req.headers['if-none-match'] = 'somethingelse'
        req.headers['content-length'] = '0'
        with set_http_connect():
            resp = req.get_response(self.app)
        self.assertEqual(resp.status_int, 400)

    def test_PUT_connect_exceptions(self):
        object_ring = self.app.get_object_ring(None)
        self.app.sort_nodes = lambda n: n  # disable shuffle

        def test_status_map(statuses, expected):
            self.app._error_limiting = {}
            req = swob.Request.blank('/v1/a/c/o.jpg', method='PUT',
                                     body='test body')
            with set_http_connect(*statuses):
                resp = req.get_response(self.app)
            self.assertEqual(resp.status_int, expected)

        base_status = [201] * 3
        # test happy path
        test_status_map(list(base_status), 201)
        for i in range(3):
            self.assertEqual(node_error_count(
                self.app, object_ring.devs[i]), 0)
        # single node errors and test isolation
        for i in range(3):
            status_list = list(base_status)
            status_list[i] = 503
            test_status_map(status_list, 201)
            for j in range(3):
                self.assertEqual(node_error_count(
                    self.app, object_ring.devs[j]), 1 if j == i else 0)
        # connect errors
        test_status_map((201, Timeout(), 201, 201), 201)
        self.assertEqual(node_error_count(
            self.app, object_ring.devs[1]), 1)
        test_status_map((Exception('kaboom!'), 201, 201, 201), 201)
        self.assertEqual(node_error_count(
            self.app, object_ring.devs[0]), 1)
        # expect errors
        test_status_map((201, 201, (503, None), 201), 201)
        self.assertEqual(node_error_count(
            self.app, object_ring.devs[2]), 1)
        test_status_map(((507, None), 201, 201, 201), 201)
        self.assertEqual(
            node_error_count(self.app, object_ring.devs[0]),
            self.app.error_suppression_limit + 1)
        # response errors
        test_status_map(((100, Timeout()), 201, 201), 201)
        self.assertEqual(
            node_error_count(self.app, object_ring.devs[0]), 1)
        test_status_map((201, 201, (100, Exception())), 201)
        self.assertEqual(
            node_error_count(self.app, object_ring.devs[2]), 1)
        test_status_map((201, (100, 507), 201), 201)
        self.assertEqual(
            node_error_count(self.app, object_ring.devs[1]),
            self.app.error_suppression_limit + 1)

    def test_PUT_connect_exception_with_unicode_path(self):
        expected = 201
        statuses = (
            Exception('Connection refused: Please insert ten dollars'),
            201, 201, 201)

        req = swob.Request.blank('/v1/AUTH_kilroy/%ED%88%8E/%E9%90%89',
                                 method='PUT',
                                 body='life is utf-gr8')
        self.app.logger.clear()
        with set_http_connect(*statuses):
            resp = req.get_response(self.app)

        self.assertEqual(resp.status_int, expected)
        log_lines = self.app.logger.get_lines_for_level('error')
        self.assertFalse(log_lines[1:])
        self.assertIn('ERROR with Object server', log_lines[0])
        self.assertIn(req.swift_entity_path.decode('utf-8'), log_lines[0])
        self.assertIn('re: Expect: 100-continue', log_lines[0])

    def test_PUT_get_expect_errors_with_unicode_path(self):
        def do_test(statuses):
            req = swob.Request.blank('/v1/AUTH_kilroy/%ED%88%8E/%E9%90%89',
                                     method='PUT',
                                     body='life is utf-gr8')
            self.app.logger.clear()
            with set_http_connect(*statuses):
                resp = req.get_response(self.app)

            self.assertEqual(resp.status_int, 201)
            log_lines = self.app.logger.get_lines_for_level('error')
            self.assertFalse(log_lines[1:])
            return log_lines

        log_lines = do_test((201, (507, None), 201, 201))
        self.assertIn('ERROR Insufficient Storage', log_lines[0])

        log_lines = do_test((201, (503, None), 201, 201))
        self.assertIn('ERROR 503 Expect: 100-continue From Object Server',
                      log_lines[0])

    def test_PUT_send_exception_with_unicode_path(self):
        def do_test(exc):
            conns = set()

            def capture_send(conn, data):
                conns.add(conn)
                if len(conns) == 2:
                    raise exc

            req = swob.Request.blank('/v1/AUTH_kilroy/%ED%88%8E/%E9%90%89',
                                     method='PUT',
                                     body='life is utf-gr8')
            self.app.logger.clear()
            with set_http_connect(201, 201, 201, give_send=capture_send):
                resp = req.get_response(self.app)

            self.assertEqual(resp.status_int, 201)
            log_lines = self.app.logger.get_lines_for_level('error')
            self.assertFalse(log_lines[1:])
            self.assertIn('ERROR with Object server', log_lines[0])
            self.assertIn(req.swift_entity_path.decode('utf-8'), log_lines[0])
            self.assertIn('Trying to write to', log_lines[0])

        do_test(Exception('Exception while sending data on connection'))
        do_test(ChunkWriteTimeout())

    def test_PUT_final_response_errors_with_unicode_path(self):
        def do_test(statuses):
            req = swob.Request.blank('/v1/AUTH_kilroy/%ED%88%8E/%E9%90%89',
                                     method='PUT',
                                     body='life is utf-gr8')
            self.app.logger.clear()
            with set_http_connect(*statuses):
                resp = req.get_response(self.app)

            self.assertEqual(resp.status_int, 201)
            log_lines = self.app.logger.get_lines_for_level('error')
            self.assertFalse(log_lines[1:])
            return req, log_lines

        req, log_lines = do_test((201, (100, Exception('boom')), 201))
        self.assertIn('ERROR with Object server', log_lines[0])
        self.assertIn(req.path.decode('utf-8'), log_lines[0])
        self.assertIn('Trying to get final status of PUT', log_lines[0])

        req, log_lines = do_test((201, (100, Timeout()), 201))
        self.assertIn('ERROR with Object server', log_lines[0])
        self.assertIn(req.path.decode('utf-8'), log_lines[0])
        self.assertIn('Trying to get final status of PUT', log_lines[0])

        req, log_lines = do_test((201, (100, 507), 201))
        self.assertIn('ERROR Insufficient Storage', log_lines[0])

        req, log_lines = do_test((201, (100, 500), 201))
        self.assertIn('ERROR 500  From Object Server', log_lines[0])
        self.assertIn(req.path.decode('utf-8'), log_lines[0])

    def test_DELETE_errors(self):
        # verify logged errors with and without non-ascii characters in path
        def do_test(path, statuses):

            req = swob.Request.blank('/v1' + path,
                                     method='DELETE',
                                     body='life is utf-gr8')
            self.app.logger.clear()
            with set_http_connect(*statuses):
                resp = req.get_response(self.app)

            self.assertEqual(resp.status_int, 201)
            log_lines = self.app.logger.get_lines_for_level('error')
            self.assertFalse(log_lines[1:])
            return req, log_lines

        req, log_lines = do_test('/AUTH_kilroy/ascii/ascii',
                                 (201, 500, 201, 201))
        self.assertIn('Trying to DELETE', log_lines[0])
        self.assertIn(req.swift_entity_path.decode('utf-8'), log_lines[0])
        self.assertIn(' From Object Server', log_lines[0])

        req, log_lines = do_test('/AUTH_kilroy/%ED%88%8E/%E9%90%89',
                                 (201, 500, 201, 201))
        self.assertIn('Trying to DELETE', log_lines[0])
        self.assertIn(req.swift_entity_path.decode('utf-8'), log_lines[0])
        self.assertIn(' From Object Server', log_lines[0])

        req, log_lines = do_test('/AUTH_kilroy/ascii/ascii',
                                 (201, 507, 201, 201))
        self.assertIn('ERROR Insufficient Storage', log_lines[0])

        req, log_lines = do_test('/AUTH_kilroy/%ED%88%8E/%E9%90%89',
                                 (201, 507, 201, 201))
        self.assertIn('ERROR Insufficient Storage', log_lines[0])

        req, log_lines = do_test('/AUTH_kilroy/ascii/ascii',
                                 (201, Exception(), 201, 201))
        self.assertIn('Trying to DELETE', log_lines[0])
        self.assertIn(req.swift_entity_path.decode('utf-8'), log_lines[0])
        self.assertIn('ERROR with Object server', log_lines[0])

        req, log_lines = do_test('/AUTH_kilroy/%ED%88%8E/%E9%90%89',
                                 (201, Exception(), 201, 201))
        self.assertIn('Trying to DELETE', log_lines[0])
        self.assertIn(req.swift_entity_path.decode('utf-8'), log_lines[0])
        self.assertIn('ERROR with Object server', log_lines[0])

    def test_PUT_error_during_transfer_data(self):
        class FakeReader(object):
            def read(self, size):
                raise IOError('error message')

        req = swob.Request.blank('/v1/a/c/o.jpg', method='PUT',
                                 body='test body')

        req.environ['wsgi.input'] = FakeReader()
        req.headers['content-length'] = '6'
        with set_http_connect(201, 201, 201):
            resp = req.get_response(self.app)

        self.assertEqual(resp.status_int, 499)

    def test_PUT_chunkreadtimeout_during_transfer_data(self):
        class FakeReader(object):
            def read(self, size):
                raise exceptions.ChunkReadTimeout()

        req = swob.Request.blank('/v1/a/c/o.jpg', method='PUT',
                                 body='test body')

        req.environ['wsgi.input'] = FakeReader()
        req.headers['content-length'] = '6'
        with set_http_connect(201, 201, 201):
            resp = req.get_response(self.app)

        self.assertEqual(resp.status_int, 408)

    def test_PUT_timeout_during_transfer_data(self):
        class FakeReader(object):
            def read(self, size):
                raise Timeout()
        conns = []

        def capture_expect(conn):
            # stash connections so that we can verify they all get closed
            conns.append(conn)

        req = swob.Request.blank('/v1/a/c/o.jpg', method='PUT',
                                 body='test body')

        req.environ['wsgi.input'] = FakeReader()
        req.headers['content-length'] = '6'
        with set_http_connect(201, 201, 201, give_expect=capture_expect):
            resp = req.get_response(self.app)

        self.assertEqual(resp.status_int, 499)
        self.assertEqual(self.replicas(), len(conns))
        for conn in conns:
            self.assertTrue(conn.closed)

    def test_PUT_exception_during_transfer_data(self):
        class FakeReader(object):
            def read(self, size):
                raise Exception('exception message')

        req = swob.Request.blank('/v1/a/c/o.jpg', method='PUT',
                                 body='test body')

        req.environ['wsgi.input'] = FakeReader()
        req.headers['content-length'] = '6'
        with set_http_connect(201, 201, 201):
            resp = req.get_response(self.app)

        self.assertEqual(resp.status_int, 500)

    def test_GET_simple(self):
        req = swift.common.swob.Request.blank('/v1/a/c/o')
        with set_http_connect(200):
            resp = req.get_response(self.app)
        self.assertEqual(resp.status_int, 200)
        self.assertIn('Accept-Ranges', resp.headers)

    def test_GET_transfer_encoding_chunked(self):
        req = swift.common.swob.Request.blank('/v1/a/c/o')
        with set_http_connect(200, headers={'transfer-encoding': 'chunked'}):
            resp = req.get_response(self.app)
        self.assertEqual(resp.status_int, 200)
        self.assertEqual(resp.headers['Transfer-Encoding'], 'chunked')

    def _test_removes_swift_bytes(self, method):
        req = swift.common.swob.Request.blank('/v1/a/c/o', method=method)
        with set_http_connect(
                200, headers={'content-type': 'image/jpeg; swift_bytes=99'}):
            resp = req.get_response(self.app)
        self.assertEqual(resp.status_int, 200)
        self.assertEqual(resp.headers['Content-Type'], 'image/jpeg')

    def test_GET_removes_swift_bytes(self):
        self._test_removes_swift_bytes('GET')

    def test_HEAD_removes_swift_bytes(self):
        self._test_removes_swift_bytes('HEAD')

    def test_GET_error(self):
        req = swift.common.swob.Request.blank('/v1/a/c/o')
        self.app.logger.txn_id = req.environ['swift.trans_id'] = 'my-txn-id'
        stdout = BytesIO()
        with set_http_connect(503, 200), \
                mock.patch('sys.stdout', stdout):
            resp = req.get_response(self.app)
        self.assertEqual(resp.status_int, 200)
        for line in stdout.getvalue().splitlines():
            self.assertIn('my-txn-id', line)
        self.assertIn('From Object Server', stdout.getvalue())

    def test_GET_handoff(self):
        req = swift.common.swob.Request.blank('/v1/a/c/o')
        codes = [503] * self.obj_ring.replicas + [200]
        with set_http_connect(*codes):
            resp = req.get_response(self.app)
        self.assertEqual(resp.status_int, 200)

    def test_GET_not_found(self):
        req = swift.common.swob.Request.blank('/v1/a/c/o')
        codes = [404] * (self.obj_ring.replicas +
                         self.obj_ring.max_more_nodes)
        with set_http_connect(*codes):
            resp = req.get_response(self.app)
        self.assertEqual(resp.status_int, 404)

    def test_PUT_delete_at(self):
        t = str(int(time.time() + 100))
        req = swob.Request.blank('/v1/a/c/o', method='PUT', body='',
                                 headers={'Content-Type': 'foo/bar',
                                          'X-Delete-At': t})
        put_headers = []

        def capture_headers(ip, port, device, part, method, path, headers,
                            **kwargs):
            if method == 'PUT':
                put_headers.append(headers)
        codes = [201] * self.obj_ring.replicas
        with set_http_connect(*codes, give_connect=capture_headers):
            resp = req.get_response(self.app)
        self.assertEqual(resp.status_int, 201)
        for given_headers in put_headers:
            self.assertEqual(given_headers.get('X-Delete-At'), t)
            self.assertIn('X-Delete-At-Host', given_headers)
            self.assertIn('X-Delete-At-Device', given_headers)
            self.assertIn('X-Delete-At-Partition', given_headers)
            self.assertIn('X-Delete-At-Container', given_headers)

    def test_PUT_converts_delete_after_to_delete_at(self):
        req = swob.Request.blank('/v1/a/c/o', method='PUT', body='',
                                 headers={'Content-Type': 'foo/bar',
                                          'X-Delete-After': '60'})
        put_headers = []

        def capture_headers(ip, port, device, part, method, path, headers,
                            **kwargs):
            if method == 'PUT':
                put_headers.append(headers)
        codes = [201] * self.obj_ring.replicas
        t = time.time()
        with set_http_connect(*codes, give_connect=capture_headers):
            with mock.patch('time.time', lambda: t):
                resp = req.get_response(self.app)
        self.assertEqual(resp.status_int, 201)
        expected_delete_at = str(int(t) + 60)
        for given_headers in put_headers:
            self.assertEqual(given_headers.get('X-Delete-At'),
                             expected_delete_at)
            self.assertIn('X-Delete-At-Host', given_headers)
            self.assertIn('X-Delete-At-Device', given_headers)
            self.assertIn('X-Delete-At-Partition', given_headers)
            self.assertIn('X-Delete-At-Container', given_headers)

    def test_container_sync_put_x_timestamp_not_found(self):
        test_indexes = [None] + [int(p) for p in POLICIES]
        for policy_index in test_indexes:
            self.app.container_info['storage_policy'] = policy_index
            put_timestamp = utils.Timestamp(time.time()).normal
            req = swob.Request.blank(
                '/v1/a/c/o', method='PUT', headers={
                    'Content-Length': 0,
                    'X-Timestamp': put_timestamp})
            codes = [201] * self.obj_ring.replicas
            with set_http_connect(*codes):
                resp = req.get_response(self.app)
            self.assertEqual(resp.status_int, 201)

    def test_container_sync_put_x_timestamp_match(self):
        test_indexes = [None] + [int(p) for p in POLICIES]
        for policy_index in test_indexes:
            self.app.container_info['storage_policy'] = policy_index
            put_timestamp = utils.Timestamp(time.time()).normal
            req = swob.Request.blank(
                '/v1/a/c/o', method='PUT', headers={
                    'Content-Length': 0,
                    'X-Timestamp': put_timestamp})
            ts_iter = itertools.repeat(put_timestamp)
            codes = [409] * self.obj_ring.replicas
            with set_http_connect(*codes, timestamps=ts_iter):
                resp = req.get_response(self.app)
            self.assertEqual(resp.status_int, 202)

    def test_container_sync_put_x_timestamp_older(self):
        ts = (utils.Timestamp(t) for t in itertools.count(int(time.time())))
        test_indexes = [None] + [int(p) for p in POLICIES]
        for policy_index in test_indexes:
            self.app.container_info['storage_policy'] = policy_index
            req = swob.Request.blank(
                '/v1/a/c/o', method='PUT', headers={
                    'Content-Length': 0,
                    'X-Timestamp': next(ts).internal})
            ts_iter = itertools.repeat(next(ts).internal)
            codes = [409] * self.obj_ring.replicas
            with set_http_connect(*codes, timestamps=ts_iter):
                resp = req.get_response(self.app)
            self.assertEqual(resp.status_int, 202)

    def test_container_sync_put_x_timestamp_newer(self):
        ts = (utils.Timestamp(t) for t in itertools.count(int(time.time())))
        test_indexes = [None] + [int(p) for p in POLICIES]
        for policy_index in test_indexes:
            orig_timestamp = next(ts).internal
            req = swob.Request.blank(
                '/v1/a/c/o', method='PUT', headers={
                    'Content-Length': 0,
                    'X-Timestamp': next(ts).internal})
            ts_iter = itertools.repeat(orig_timestamp)
            codes = [201] * self.obj_ring.replicas
            with set_http_connect(*codes, timestamps=ts_iter):
                resp = req.get_response(self.app)
            self.assertEqual(resp.status_int, 201)

    def test_put_x_timestamp_conflict(self):
        ts = (utils.Timestamp(t) for t in itertools.count(int(time.time())))
        req = swob.Request.blank(
            '/v1/a/c/o', method='PUT', headers={
                'Content-Length': 0,
                'X-Timestamp': next(ts).internal})
        ts_iter = iter([next(ts).internal, None, None])
        codes = [409] + [201] * (self.obj_ring.replicas - 1)
        with set_http_connect(*codes, timestamps=ts_iter):
            resp = req.get_response(self.app)
        self.assertEqual(resp.status_int, 202)

    def test_put_x_timestamp_conflict_with_missing_backend_timestamp(self):
        ts = (utils.Timestamp(t) for t in itertools.count(int(time.time())))
        req = swob.Request.blank(
            '/v1/a/c/o', method='PUT', headers={
                'Content-Length': 0,
                'X-Timestamp': next(ts).internal})
        ts_iter = iter([None, None, None])
        codes = [409] * self.obj_ring.replicas
        with set_http_connect(*codes, timestamps=ts_iter):
            resp = req.get_response(self.app)
        self.assertEqual(resp.status_int, 202)

    def test_put_x_timestamp_conflict_with_other_weird_success_response(self):
        ts = (utils.Timestamp(t) for t in itertools.count(int(time.time())))
        req = swob.Request.blank(
            '/v1/a/c/o', method='PUT', headers={
                'Content-Length': 0,
                'X-Timestamp': next(ts).internal})
        ts_iter = iter([next(ts).internal, None, None])
        codes = [409] + [(201, 'notused')] * (self.obj_ring.replicas - 1)
        with set_http_connect(*codes, timestamps=ts_iter):
            resp = req.get_response(self.app)
        self.assertEqual(resp.status_int, 202)

    def test_put_x_timestamp_conflict_with_if_none_match(self):
        ts = (utils.Timestamp(t) for t in itertools.count(int(time.time())))
        req = swob.Request.blank(
            '/v1/a/c/o', method='PUT', headers={
                'Content-Length': 0,
                'If-None-Match': '*',
                'X-Timestamp': next(ts).internal})
        ts_iter = iter([next(ts).internal, None, None])
        codes = [409] + [(412, 'notused')] * (self.obj_ring.replicas - 1)
        with set_http_connect(*codes, timestamps=ts_iter):
            resp = req.get_response(self.app)
        self.assertEqual(resp.status_int, 412)

    def test_container_sync_put_x_timestamp_race(self):
        ts = (utils.Timestamp(t) for t in itertools.count(int(time.time())))
        test_indexes = [None] + [int(p) for p in POLICIES]
        for policy_index in test_indexes:
            put_timestamp = next(ts).internal
            req = swob.Request.blank(
                '/v1/a/c/o', method='PUT', headers={
                    'Content-Length': 0,
                    'X-Timestamp': put_timestamp})

            # object nodes they respond 409 because another in-flight request
            # finished and now the on disk timestamp is equal to the request.
            put_ts = [put_timestamp] * self.obj_ring.replicas
            codes = [409] * self.obj_ring.replicas

            ts_iter = iter(put_ts)
            with set_http_connect(*codes, timestamps=ts_iter):
                resp = req.get_response(self.app)
            self.assertEqual(resp.status_int, 202)

    def test_container_sync_put_x_timestamp_unsynced_race(self):
        ts = (utils.Timestamp(t) for t in itertools.count(int(time.time())))
        test_indexes = [None] + [int(p) for p in POLICIES]
        for policy_index in test_indexes:
            put_timestamp = next(ts).internal
            req = swob.Request.blank(
                '/v1/a/c/o', method='PUT', headers={
                    'Content-Length': 0,
                    'X-Timestamp': put_timestamp})

            # only one in-flight request finished
            put_ts = [None] * (self.obj_ring.replicas - 1)
            put_resp = [201] * (self.obj_ring.replicas - 1)
            put_ts += [put_timestamp]
            put_resp += [409]

            ts_iter = iter(put_ts)
            codes = put_resp
            with set_http_connect(*codes, timestamps=ts_iter):
                resp = req.get_response(self.app)
            self.assertEqual(resp.status_int, 202)


@patch_policies(
    [StoragePolicy(0, '1-replica', True),
     StoragePolicy(1, '5-replica', False),
     StoragePolicy(2, '8-replica', False),
     StoragePolicy(3, '15-replica', False)],
    fake_ring_args=[
        {'replicas': 1}, {'replicas': 5}, {'replicas': 8}, {'replicas': 15}])
class TestReplicatedObjControllerVariousReplicas(BaseObjectControllerMixin,
                                                 unittest.TestCase):
    controller_cls = obj.ReplicatedObjectController


class StubResponse(object):

    def __init__(self, status, body='', headers=None, frag_index=None):
        self.status = status
        self.body = body
        self.readable = BytesIO(body)
        self.headers = HeaderKeyDict(headers)
        if frag_index is not None:
            self.headers['X-Object-Sysmeta-Ec-Frag-Index'] = frag_index
        fake_reason = ('Fake', 'This response is a lie.')
        self.reason = swob.RESPONSE_REASONS.get(status, fake_reason)[0]

    def getheader(self, header_name, default=None):
        return self.headers.get(header_name, default)

    def getheaders(self):
        if 'Content-Length' not in self.headers:
            self.headers['Content-Length'] = len(self.body)
        return self.headers.items()

    def read(self, amt=0):
        return self.readable.read(amt)


@contextmanager
def capture_http_requests(get_response):

    class FakeConn(object):

        def __init__(self, req):
            self.req = req
            self.resp = None

        def getresponse(self):
            self.resp = get_response(self.req)
            return self.resp

    class ConnectionLog(object):

        def __init__(self):
            self.connections = []

        def __len__(self):
            return len(self.connections)

        def __getitem__(self, i):
            return self.connections[i]

        def __iter__(self):
            return iter(self.connections)

        def __call__(self, ip, port, method, path, headers, qs, ssl):
            req = {
                'ip': ip,
                'port': port,
                'method': method,
                'path': path,
                'headers': headers,
                'qs': qs,
                'ssl': ssl,
            }
            conn = FakeConn(req)
            self.connections.append(conn)
            return conn

    fake_conn = ConnectionLog()

    with mock.patch('swift.common.bufferedhttp.http_connect_raw',
                    new=fake_conn):
        yield fake_conn


@patch_policies(with_ec_default=True)
class TestECObjController(BaseObjectControllerMixin, unittest.TestCase):
    container_info = {
        'status': 200,
        'read_acl': None,
        'write_acl': None,
        'sync_key': None,
        'versions': None,
        'storage_policy': '0',
    }

    controller_cls = obj.ECObjectController

    def _add_frag_index(self, index, headers):
        # helper method to add a frag index header to an existing header dict
        hdr_name = 'X-Object-Sysmeta-Ec-Frag-Index'
        return dict(headers.items() + [(hdr_name, index)])

    def test_determine_chunk_destinations(self):
        class FakePutter(object):
            def __init__(self, index):
                self.node_index = index

        controller = self.controller_cls(
            self.app, 'a', 'c', 'o')

        # create a dummy list of putters, check no handoffs
        putters = []
        for index in range(0, 4):
            putters.append(FakePutter(index))
        got = controller._determine_chunk_destinations(putters)
        expected = {}
        for i, p in enumerate(putters):
            expected[p] = i
        self.assertEqual(got, expected)

        # now lets make a handoff at the end
        putters[3].node_index = None
        got = controller._determine_chunk_destinations(putters)
        self.assertEqual(got, expected)
        putters[3].node_index = 3

        # now lets make a handoff at the start
        putters[0].node_index = None
        got = controller._determine_chunk_destinations(putters)
        self.assertEqual(got, expected)
        putters[0].node_index = 0

        # now lets make a handoff in the middle
        putters[2].node_index = None
        got = controller._determine_chunk_destinations(putters)
        self.assertEqual(got, expected)
        putters[2].node_index = 0

        # now lets make all of them handoffs
        for index in range(0, 4):
            putters[index].node_index = None
        got = controller._determine_chunk_destinations(putters)
        self.assertEqual(got, expected)

    def test_GET_simple(self):
        req = swift.common.swob.Request.blank('/v1/a/c/o')
        get_resp = [200] * self.policy.ec_ndata
        with set_http_connect(*get_resp):
            resp = req.get_response(self.app)
        self.assertEqual(resp.status_int, 200)
        self.assertIn('Accept-Ranges', resp.headers)

    def _test_if_match(self, method):
        num_responses = self.policy.ec_ndata if method == 'GET' else 1

        def _do_test(match_value, backend_status,
                     etag_is_at='X-Object-Sysmeta-Does-Not-Exist'):
            req = swift.common.swob.Request.blank(
                '/v1/a/c/o', method=method,
                headers={'If-Match': match_value,
                         'X-Backend-Etag-Is-At': etag_is_at})
            get_resp = [backend_status] * num_responses
            resp_headers = {'Etag': 'frag_etag',
                            'X-Object-Sysmeta-Ec-Etag': 'data_etag',
                            'X-Object-Sysmeta-Alternate-Etag': 'alt_etag'}
            with set_http_connect(*get_resp, headers=resp_headers):
                resp = req.get_response(self.app)
            self.assertEqual('data_etag', resp.headers['Etag'])
            return resp

        # wildcard
        resp = _do_test('*', 200)
        self.assertEqual(resp.status_int, 200)

        # match
        resp = _do_test('"data_etag"', 200)
        self.assertEqual(resp.status_int, 200)

        # no match
        resp = _do_test('"frag_etag"', 412)
        self.assertEqual(resp.status_int, 412)

        # match wildcard against an alternate etag
        resp = _do_test('*', 200,
                        etag_is_at='X-Object-Sysmeta-Alternate-Etag')
        self.assertEqual(resp.status_int, 200)

        # match against an alternate etag
        resp = _do_test('"alt_etag"', 200,
                        etag_is_at='X-Object-Sysmeta-Alternate-Etag')
        self.assertEqual(resp.status_int, 200)

        # no match against an alternate etag
        resp = _do_test('"data_etag"', 412,
                        etag_is_at='X-Object-Sysmeta-Alternate-Etag')
        self.assertEqual(resp.status_int, 412)

    def test_GET_if_match(self):
        self._test_if_match('GET')

    def test_HEAD_if_match(self):
        self._test_if_match('HEAD')

    def _test_if_none_match(self, method):
        num_responses = self.policy.ec_ndata if method == 'GET' else 1

        def _do_test(match_value, backend_status,
                     etag_is_at='X-Object-Sysmeta-Does-Not-Exist'):
            req = swift.common.swob.Request.blank(
                '/v1/a/c/o', method=method,
                headers={'If-None-Match': match_value,
                         'X-Backend-Etag-Is-At': etag_is_at})
            get_resp = [backend_status] * num_responses
            resp_headers = {'Etag': 'frag_etag',
                            'X-Object-Sysmeta-Ec-Etag': 'data_etag',
                            'X-Object-Sysmeta-Alternate-Etag': 'alt_etag'}
            with set_http_connect(*get_resp, headers=resp_headers):
                resp = req.get_response(self.app)
            self.assertEqual('data_etag', resp.headers['Etag'])
            return resp

        # wildcard
        resp = _do_test('*', 304)
        self.assertEqual(resp.status_int, 304)

        # match
        resp = _do_test('"data_etag"', 304)
        self.assertEqual(resp.status_int, 304)

        # no match
        resp = _do_test('"frag_etag"', 200)
        self.assertEqual(resp.status_int, 200)

        # match wildcard against an alternate etag
        resp = _do_test('*', 304,
                        etag_is_at='X-Object-Sysmeta-Alternate-Etag')
        self.assertEqual(resp.status_int, 304)

        # match against an alternate etag
        resp = _do_test('"alt_etag"', 304,
                        etag_is_at='X-Object-Sysmeta-Alternate-Etag')
        self.assertEqual(resp.status_int, 304)

        # no match against an alternate etag
        resp = _do_test('"data_etag"', 200,
                        etag_is_at='X-Object-Sysmeta-Alternate-Etag')
        self.assertEqual(resp.status_int, 200)

    def test_GET_if_none_match(self):
        self._test_if_none_match('GET')

    def test_HEAD_if_none_match(self):
        self._test_if_none_match('HEAD')

    def test_GET_simple_x_newest(self):
        req = swift.common.swob.Request.blank('/v1/a/c/o',
                                              headers={'X-Newest': 'true'})
        codes = [200] * self.policy.ec_ndata
        with set_http_connect(*codes):
            resp = req.get_response(self.app)
        self.assertEqual(resp.status_int, 200)

    def test_GET_error(self):
        req = swift.common.swob.Request.blank('/v1/a/c/o')
        get_resp = [503] + [200] * self.policy.ec_ndata
        with set_http_connect(*get_resp):
            resp = req.get_response(self.app)
        self.assertEqual(resp.status_int, 200)

    def test_GET_with_body(self):
        req = swift.common.swob.Request.blank('/v1/a/c/o')
        # turn a real body into fragments
        segment_size = self.policy.ec_segment_size
        real_body = ('asdf' * segment_size)[:-10]
        # split it up into chunks
        chunks = [real_body[x:x + segment_size]
                  for x in range(0, len(real_body), segment_size)]
        fragment_payloads = []
        for chunk in chunks:
            fragments = self.policy.pyeclib_driver.encode(chunk)
            if not fragments:
                break
            fragment_payloads.append(fragments)
        # sanity
        sanity_body = ''
        for fragment_payload in fragment_payloads:
            sanity_body += self.policy.pyeclib_driver.decode(
                fragment_payload)
        self.assertEqual(len(real_body), len(sanity_body))
        self.assertEqual(real_body, sanity_body)

        # list(zip(...)) for py3 compatibility (zip is lazy there)
        node_fragments = list(zip(*fragment_payloads))
        self.assertEqual(len(node_fragments), self.replicas())  # sanity
        headers = {'X-Object-Sysmeta-Ec-Content-Length': str(len(real_body))}
        responses = [(200, ''.join(node_fragments[i]), headers)
                     for i in range(POLICIES.default.ec_ndata)]
        status_codes, body_iter, headers = zip(*responses)
        with set_http_connect(*status_codes, body_iter=body_iter,
                              headers=headers):
            resp = req.get_response(self.app)
        self.assertEqual(resp.status_int, 200)
        self.assertEqual(len(real_body), len(resp.body))
        self.assertEqual(real_body, resp.body)

    def test_PUT_simple(self):
        req = swift.common.swob.Request.blank('/v1/a/c/o', method='PUT',
                                              body='')
        codes = [201] * self.replicas()
        expect_headers = {
            'X-Obj-Metadata-Footer': 'yes',
            'X-Obj-Multiphase-Commit': 'yes'
        }
        with set_http_connect(*codes, expect_headers=expect_headers):
            resp = req.get_response(self.app)
        self.assertEqual(resp.status_int, 201)

    def test_PUT_with_body_and_bad_etag(self):
        segment_size = self.policy.ec_segment_size
        test_body = ('asdf' * segment_size)[:-10]
        codes = [201] * self.replicas()
        expect_headers = {
            'X-Obj-Metadata-Footer': 'yes',
            'X-Obj-Multiphase-Commit': 'yes'
        }
        conns = []

        def capture_expect(conn):
            # stash the backend connection so we can verify that it is closed
            # (no data will be sent)
            conns.append(conn)

        # send a bad etag in the request headers
        headers = {'Etag': 'bad etag'}
        req = swift.common.swob.Request.blank(
            '/v1/a/c/o', method='PUT', headers=headers, body=test_body)
        with set_http_connect(*codes, expect_headers=expect_headers,
                              give_expect=capture_expect):
            resp = req.get_response(self.app)
        self.assertEqual(422, resp.status_int)
        self.assertEqual(self.replicas(), len(conns))
        for conn in conns:
            self.assertTrue(conn.closed)

        # make the footers callback send a bad Etag footer
        footers_callback = make_footers_callback('not the test body')
        env = {'swift.callback.update_footers': footers_callback}
        req = swift.common.swob.Request.blank(
            '/v1/a/c/o', method='PUT', environ=env, body=test_body)
        with set_http_connect(*codes, expect_headers=expect_headers):
            resp = req.get_response(self.app)
        self.assertEqual(422, resp.status_int)

    def test_txn_id_logging_ECPUT(self):
        req = swift.common.swob.Request.blank('/v1/a/c/o', method='PUT',
                                              body='')
        self.app.logger.txn_id = req.environ['swift.trans_id'] = 'test-txn-id'
        codes = [(100, Timeout(), 503, 503)] * self.replicas()
        stdout = BytesIO()
        expect_headers = {
            'X-Obj-Metadata-Footer': 'yes',
            'X-Obj-Multiphase-Commit': 'yes'
        }
        with set_http_connect(*codes, expect_headers=expect_headers), \
                mock.patch('sys.stdout', stdout):
            resp = req.get_response(self.app)
        self.assertEqual(resp.status_int, 503)
        for line in stdout.getvalue().splitlines():
            self.assertIn('test-txn-id', line)
        self.assertIn('Trying to get ',
                      stdout.getvalue())

    def test_PUT_with_explicit_commit_status(self):
        req = swift.common.swob.Request.blank('/v1/a/c/o', method='PUT',
                                              body='')
        codes = [(100, 100, 201)] * self.replicas()
        expect_headers = {
            'X-Obj-Metadata-Footer': 'yes',
            'X-Obj-Multiphase-Commit': 'yes'
        }
        with set_http_connect(*codes, expect_headers=expect_headers):
            resp = req.get_response(self.app)
        self.assertEqual(resp.status_int, 201)

    def test_PUT_error(self):
        req = swift.common.swob.Request.blank('/v1/a/c/o', method='PUT',
                                              body='')
        codes = [503] * self.replicas()
        expect_headers = {
            'X-Obj-Metadata-Footer': 'yes',
            'X-Obj-Multiphase-Commit': 'yes'
        }
        with set_http_connect(*codes, expect_headers=expect_headers):
            resp = req.get_response(self.app)
        self.assertEqual(resp.status_int, 503)

    def test_PUT_mostly_success(self):
        req = swift.common.swob.Request.blank('/v1/a/c/o', method='PUT',
                                              body='')
        codes = [201] * self.quorum()
        codes += [503] * (self.replicas() - len(codes))
        random.shuffle(codes)
        expect_headers = {
            'X-Obj-Metadata-Footer': 'yes',
            'X-Obj-Multiphase-Commit': 'yes'
        }
        with set_http_connect(*codes, expect_headers=expect_headers):
            resp = req.get_response(self.app)
        self.assertEqual(resp.status_int, 201)

    def test_PUT_error_commit(self):
        req = swift.common.swob.Request.blank('/v1/a/c/o', method='PUT',
                                              body='')
        codes = [(100, 503, Exception('not used'))] * self.replicas()
        expect_headers = {
            'X-Obj-Metadata-Footer': 'yes',
            'X-Obj-Multiphase-Commit': 'yes'
        }
        with set_http_connect(*codes, expect_headers=expect_headers):
            resp = req.get_response(self.app)
        self.assertEqual(resp.status_int, 503)

    def test_PUT_mostly_success_commit(self):
        req = swift.common.swob.Request.blank('/v1/a/c/o', method='PUT',
                                              body='')
        codes = [201] * self.quorum()
        codes += [(100, 503, Exception('not used'))] * (
            self.replicas() - len(codes))
        random.shuffle(codes)
        expect_headers = {
            'X-Obj-Metadata-Footer': 'yes',
            'X-Obj-Multiphase-Commit': 'yes'
        }
        with set_http_connect(*codes, expect_headers=expect_headers):
            resp = req.get_response(self.app)
        self.assertEqual(resp.status_int, 201)

    def test_PUT_mostly_error_commit(self):
        req = swift.common.swob.Request.blank('/v1/a/c/o', method='PUT',
                                              body='')
        codes = [(100, 503, Exception('not used'))] * self.quorum()
        codes += [201] * (self.replicas() - len(codes))
        random.shuffle(codes)
        expect_headers = {
            'X-Obj-Metadata-Footer': 'yes',
            'X-Obj-Multiphase-Commit': 'yes'
        }
        with set_http_connect(*codes, expect_headers=expect_headers):
            resp = req.get_response(self.app)
        self.assertEqual(resp.status_int, 503)

    def test_PUT_commit_timeout(self):
        req = swift.common.swob.Request.blank('/v1/a/c/o', method='PUT',
                                              body='')
        codes = [201] * (self.replicas() - 1)
        codes.append((100, Timeout(), Exception('not used')))
        expect_headers = {
            'X-Obj-Metadata-Footer': 'yes',
            'X-Obj-Multiphase-Commit': 'yes'
        }
        with set_http_connect(*codes, expect_headers=expect_headers):
            resp = req.get_response(self.app)
        self.assertEqual(resp.status_int, 201)

    def test_PUT_commit_exception(self):
        req = swift.common.swob.Request.blank('/v1/a/c/o', method='PUT',
                                              body='')
        codes = [201] * (self.replicas() - 1)
        codes.append((100, Exception('kaboom!'), Exception('not used')))
        expect_headers = {
            'X-Obj-Metadata-Footer': 'yes',
            'X-Obj-Multiphase-Commit': 'yes'
        }
        with set_http_connect(*codes, expect_headers=expect_headers):
            resp = req.get_response(self.app)
        self.assertEqual(resp.status_int, 201)

    def test_PUT_ec_error_during_transfer_data(self):
        class FakeReader(object):
            def read(self, size):
                raise IOError('error message')

        req = swob.Request.blank('/v1/a/c/o.jpg', method='PUT',
                                 body='test body')

        req.environ['wsgi.input'] = FakeReader()
        req.headers['content-length'] = '6'
        codes = [201] * self.replicas()
        expect_headers = {
            'X-Obj-Metadata-Footer': 'yes',
            'X-Obj-Multiphase-Commit': 'yes'
        }
        with set_http_connect(*codes, expect_headers=expect_headers):
            resp = req.get_response(self.app)

        self.assertEqual(resp.status_int, 499)

    def test_PUT_ec_chunkreadtimeout_during_transfer_data(self):
        class FakeReader(object):
            def read(self, size):
                raise exceptions.ChunkReadTimeout()

        req = swob.Request.blank('/v1/a/c/o.jpg', method='PUT',
                                 body='test body')

        req.environ['wsgi.input'] = FakeReader()
        req.headers['content-length'] = '6'
        codes = [201] * self.replicas()
        expect_headers = {
            'X-Obj-Metadata-Footer': 'yes',
            'X-Obj-Multiphase-Commit': 'yes'
        }
        with set_http_connect(*codes, expect_headers=expect_headers):
            resp = req.get_response(self.app)

        self.assertEqual(resp.status_int, 408)

    def test_PUT_ec_timeout_during_transfer_data(self):
        class FakeReader(object):
            def read(self, size):
                raise exceptions.Timeout()

        req = swob.Request.blank('/v1/a/c/o.jpg', method='PUT',
                                 body='test body')

        req.environ['wsgi.input'] = FakeReader()
        req.headers['content-length'] = '6'
        codes = [201] * self.replicas()
        expect_headers = {
            'X-Obj-Metadata-Footer': 'yes',
            'X-Obj-Multiphase-Commit': 'yes'
        }
        with set_http_connect(*codes, expect_headers=expect_headers):
            resp = req.get_response(self.app)

        self.assertEqual(resp.status_int, 499)

    def test_PUT_ec_exception_during_transfer_data(self):
        class FakeReader(object):
            def read(self, size):
                raise Exception('exception message')

        req = swob.Request.blank('/v1/a/c/o.jpg', method='PUT',
                                 body='test body')

        req.environ['wsgi.input'] = FakeReader()
        req.headers['content-length'] = '6'
        codes = [201] * self.replicas()
        expect_headers = {
            'X-Obj-Metadata-Footer': 'yes',
            'X-Obj-Multiphase-Commit': 'yes'
        }
        with set_http_connect(*codes, expect_headers=expect_headers):
            resp = req.get_response(self.app)

        self.assertEqual(resp.status_int, 500)

    def test_PUT_with_body(self):
        segment_size = self.policy.ec_segment_size
        test_body = ('asdf' * segment_size)[:-10]
        # make the footers callback not include Etag footer so that we can
        # verify that the correct EC-calculated Etag is included in footers
        # sent to backend
        footers_callback = make_footers_callback()
        env = {'swift.callback.update_footers': footers_callback}
        req = swift.common.swob.Request.blank(
            '/v1/a/c/o', method='PUT', environ=env)
        etag = md5(test_body).hexdigest()
        size = len(test_body)
        req.body = test_body
        codes = [201] * self.replicas()
        expect_headers = {
            'X-Obj-Metadata-Footer': 'yes',
            'X-Obj-Multiphase-Commit': 'yes'
        }
        resp_headers = {
            'Some-Other-Header': 'Four',
            'Etag': 'ignored',
        }

        put_requests = defaultdict(lambda: {'boundary': None, 'chunks': []})

        def capture_body(conn, chunk):
            put_requests[conn.connection_id]['chunks'].append(chunk)

        def capture_headers(ip, port, device, part, method, path, headers,
                            **kwargs):
            conn_id = kwargs['connection_id']
            put_requests[conn_id]['boundary'] = headers[
                'X-Backend-Obj-Multipart-Mime-Boundary']
            put_requests[conn_id]['backend-content-length'] = headers[
                'X-Backend-Obj-Content-Length']
            put_requests[conn_id]['x-timestamp'] = headers[
                'X-Timestamp']

        with set_http_connect(*codes, expect_headers=expect_headers,
                              give_send=capture_body,
                              give_connect=capture_headers,
                              headers=resp_headers):
            resp = req.get_response(self.app)

        self.assertEqual(resp.status_int, 201)
        timestamps = {captured_req['x-timestamp']
                      for captured_req in put_requests.values()}
        self.assertEqual(1, len(timestamps), timestamps)
        self.assertEqual(dict(resp.headers), {
            'Content-Type': 'text/html; charset=UTF-8',
            'Content-Length': '0',
            'Last-Modified': time.strftime(
                "%a, %d %b %Y %H:%M:%S GMT",
                time.gmtime(math.ceil(float(timestamps.pop())))),
            'Etag': etag,
        })
        frag_archives = []
        for connection_id, info in put_requests.items():
            body = unchunk_body(''.join(info['chunks']))
            self.assertIsNotNone(info['boundary'],
                                 "didn't get boundary for conn %r" % (
                                     connection_id,))
            self.assertTrue(size > int(info['backend-content-length']) > 0,
                            "invalid backend-content-length for conn %r" % (
                                connection_id,))

            # email.parser.FeedParser doesn't know how to take a multipart
            # message and boundary together and parse it; it only knows how
            # to take a string, parse the headers, and figure out the
            # boundary on its own.
            parser = email.parser.FeedParser()
            parser.feed(
                "Content-Type: multipart/nobodycares; boundary=%s\r\n\r\n" %
                info['boundary'])
            parser.feed(body)
            message = parser.close()

            self.assertTrue(message.is_multipart())  # sanity check
            mime_parts = message.get_payload()
            self.assertEqual(len(mime_parts), 3)
            obj_part, footer_part, commit_part = mime_parts

            # attach the body to frag_archives list
            self.assertEqual(obj_part['X-Document'], 'object body')
            frag_archives.append(obj_part.get_payload())

            # assert length was correct for this connection
            self.assertEqual(int(info['backend-content-length']),
                             len(frag_archives[-1]))
            # assert length was the same for all connections
            self.assertEqual(int(info['backend-content-length']),
                             len(frag_archives[0]))

            # validate some footer metadata
            self.assertEqual(footer_part['X-Document'], 'object metadata')
            footer_metadata = json.loads(footer_part.get_payload())
            self.assertTrue(footer_metadata)
            expected = {}
            # update expected with footers from the callback...
            footers_callback(expected)
            expected.update({
                'X-Object-Sysmeta-Ec-Content-Length': str(size),
                'X-Backend-Container-Update-Override-Size': str(size),
                'X-Object-Sysmeta-Ec-Etag': etag,
                'X-Backend-Container-Update-Override-Etag': etag,
                'X-Object-Sysmeta-Ec-Segment-Size': str(segment_size),
                'Etag': md5(obj_part.get_payload()).hexdigest()})
            for header, value in expected.items():
                self.assertEqual(footer_metadata[header], value)

            # sanity on commit message
            self.assertEqual(commit_part['X-Document'], 'put commit')

        self.assertEqual(len(frag_archives), self.replicas())
        fragment_size = self.policy.fragment_size
        node_payloads = []
        for fa in frag_archives:
            payload = [fa[x:x + fragment_size]
                       for x in range(0, len(fa), fragment_size)]
            node_payloads.append(payload)
        fragment_payloads = zip(*node_payloads)

        expected_body = ''
        for fragment_payload in fragment_payloads:
            self.assertEqual(len(fragment_payload), self.replicas())
            if True:
                fragment_payload = list(fragment_payload)
            expected_body += self.policy.pyeclib_driver.decode(
                fragment_payload)

        self.assertEqual(len(test_body), len(expected_body))
        self.assertEqual(test_body, expected_body)

    def test_PUT_with_footers(self):
        # verify footers supplied by a footers callback being added to
        # trailing metadata
        segment_size = self.policy.ec_segment_size
        test_body = ('asdf' * segment_size)[:-10]
        etag = md5(test_body).hexdigest()
        size = len(test_body)
        codes = [201] * self.replicas()
        expect_headers = {
            'X-Obj-Metadata-Footer': 'yes',
            'X-Obj-Multiphase-Commit': 'yes'
        }
        resp_headers = {
            'Some-Other-Header': 'Four',
            'Etag': 'ignored',
        }

        def do_test(footers_to_add, expect_added):
            put_requests = defaultdict(
                lambda: {'boundary': None, 'chunks': []})

            def capture_body(conn, chunk):
                put_requests[conn.connection_id]['chunks'].append(chunk)

            def capture_headers(ip, port, device, part, method, path, headers,
                                **kwargs):
                conn_id = kwargs['connection_id']
                put_requests[conn_id]['boundary'] = headers[
                    'X-Backend-Obj-Multipart-Mime-Boundary']
                put_requests[conn_id]['x-timestamp'] = headers[
                    'X-Timestamp']

            def footers_callback(footers):
                footers.update(footers_to_add)
            env = {'swift.callback.update_footers': footers_callback}
            req = swift.common.swob.Request.blank(
                '/v1/a/c/o', method='PUT', environ=env, body=test_body)

            with set_http_connect(*codes, expect_headers=expect_headers,
                                  give_send=capture_body,
                                  give_connect=capture_headers,
                                  headers=resp_headers):
                resp = req.get_response(self.app)

            self.assertEqual(resp.status_int, 201)
            timestamps = {captured_req['x-timestamp']
                          for captured_req in put_requests.values()}
            self.assertEqual(1, len(timestamps), timestamps)
            self.assertEqual(dict(resp.headers), {
                'Content-Type': 'text/html; charset=UTF-8',
                'Content-Length': '0',
                'Last-Modified': time.strftime(
                    "%a, %d %b %Y %H:%M:%S GMT",
                    time.gmtime(math.ceil(float(timestamps.pop())))),
                'Etag': etag,
            })
            for connection_id, info in put_requests.items():
                body = unchunk_body(''.join(info['chunks']))
                # email.parser.FeedParser doesn't know how to take a multipart
                # message and boundary together and parse it; it only knows how
                # to take a string, parse the headers, and figure out the
                # boundary on its own.
                parser = email.parser.FeedParser()
                parser.feed(
                    "Content-Type: multipart/nobodycares; boundary=%s\r\n\r\n"
                    % info['boundary'])
                parser.feed(body)
                message = parser.close()

                self.assertTrue(message.is_multipart())  # sanity check
                mime_parts = message.get_payload()
                self.assertEqual(len(mime_parts), 3)
                obj_part, footer_part, commit_part = mime_parts

                # validate EC footer metadata - should always be present
                self.assertEqual(footer_part['X-Document'], 'object metadata')
                footer_metadata = json.loads(footer_part.get_payload())
                self.assertIsNotNone(
                    footer_metadata.pop('X-Object-Sysmeta-Ec-Frag-Index'))
                expected = {
                    'X-Object-Sysmeta-Ec-Scheme':
                        self.policy.ec_scheme_description,
                    'X-Object-Sysmeta-Ec-Content-Length': str(size),
                    'X-Object-Sysmeta-Ec-Etag': etag,
                    'X-Object-Sysmeta-Ec-Segment-Size': str(segment_size),
                    'Etag': md5(obj_part.get_payload()).hexdigest()}
                expected.update(expect_added)
                for header, value in expected.items():
                    self.assertIn(header, footer_metadata)
                    self.assertEqual(value, footer_metadata[header])
                    footer_metadata.pop(header)
                self.assertFalse(footer_metadata)

        # sanity check - middleware sets no footer, expect EC overrides
        footers_to_add = {}
        expect_added = {
            'X-Backend-Container-Update-Override-Size': str(size),
            'X-Backend-Container-Update-Override-Etag': etag}
        do_test(footers_to_add, expect_added)

        # middleware cannot overwrite any EC sysmeta
        footers_to_add = {
            'X-Object-Sysmeta-Ec-Content-Length': str(size + 1),
            'X-Object-Sysmeta-Ec-Etag': 'other etag',
            'X-Object-Sysmeta-Ec-Segment-Size': str(segment_size + 1),
            'X-Object-Sysmeta-Ec-Unused-But-Reserved': 'ignored'}
        do_test(footers_to_add, expect_added)

        # middleware can add x-object-sysmeta- headers including
        # x-object-sysmeta-container-update-override headers
        footers_to_add = {
            'X-Object-Sysmeta-Foo': 'bar',
            'X-Object-Sysmeta-Container-Update-Override-Size':
                str(size + 1),
            'X-Object-Sysmeta-Container-Update-Override-Etag': 'other etag',
            'X-Object-Sysmeta-Container-Update-Override-Ping': 'pong'
        }
        expect_added.update(footers_to_add)
        do_test(footers_to_add, expect_added)

        # middleware can also overwrite x-backend-container-update-override
        # headers
        override_footers = {
            'X-Backend-Container-Update-Override-Wham': 'bam',
            'X-Backend-Container-Update-Override-Size': str(size + 2),
            'X-Backend-Container-Update-Override-Etag': 'another etag'}
        footers_to_add.update(override_footers)
        expect_added.update(override_footers)
        do_test(footers_to_add, expect_added)

    def test_PUT_old_obj_server(self):
        req = swift.common.swob.Request.blank('/v1/a/c/o', method='PUT',
                                              body='')
        responses = [
            # one server will response 100-continue but not include the
            # needful expect headers and the connection will be dropped
            ((100, Exception('not used')), {}),
        ] + [
            # and pleanty of successful responses too
            (201, {
                'X-Obj-Metadata-Footer': 'yes',
                'X-Obj-Multiphase-Commit': 'yes',
            }),
        ] * self.replicas()
        random.shuffle(responses)
        if responses[-1][0] != 201:
            # whoops, stupid random
            responses = responses[1:] + [responses[0]]
        codes, expect_headers = zip(*responses)
        with set_http_connect(*codes, expect_headers=expect_headers):
            resp = req.get_response(self.app)
        self.assertEqual(resp.status_int, 201)

    def _make_ec_archive_bodies(self, test_body, policy=None):
        policy = policy or self.policy
        return encode_frag_archive_bodies(policy, test_body)

    def _make_ec_object_stub(self, test_body=None, policy=None,
                             timestamp=None):
        policy = policy or self.policy
        segment_size = policy.ec_segment_size
        test_body = test_body or (
            'test' * segment_size)[:-random.randint(1, 1000)]
        timestamp = timestamp or Timestamp(time.time())
        etag = md5(test_body).hexdigest()
        ec_archive_bodies = self._make_ec_archive_bodies(test_body,
                                                         policy=policy)
        return {
            'body': test_body,
            'etag': etag,
            'frags': ec_archive_bodies,
            'timestamp': timestamp
        }

    def _fake_ec_node_response(self, node_frags):
        """
        Given a list of entries for each node in ring order, where the entries
        are a dict (or list of dicts) which describes the fragment (or
        fragments) that are on the node; create a function suitable for use
        with capture_http_requests that will accept a req object and return a
        response that will suitably fake the behavior of an object server who
        had the given fragments on disk at the time.

        :param node_frags: a list. Each item in the list describes the
            fragments that are on a node; each item is a dict or list of dicts,
            each dict describing a single fragment; where the item is a list,
            repeated calls to get_response will return fragments in the order
            of the list; each dict has keys:
                - obj: an object stub, as generated by _make_ec_object_stub,
                    that defines all of the fragments that compose an object
                    at a specific timestamp.
                - frag: the index of a fragment to be selected from the object
                    stub
                - durable (optional): True if the selected fragment is durable

        """
        node_map = {}  # maps node ip and port to node index
        all_nodes = []
        call_count = {}  # maps node index to get_response call count for node

        def _build_node_map(req):
            node_key = lambda n: (n['ip'], n['port'])
            part = utils.split_path(req['path'], 5, 5, True)[1]
            policy = POLICIES[int(
                req['headers']['X-Backend-Storage-Policy-Index'])]
            all_nodes.extend(policy.object_ring.get_part_nodes(part))
            all_nodes.extend(policy.object_ring.get_more_nodes(part))
            for i, node in enumerate(all_nodes):
                node_map[node_key(node)] = i
                call_count[i] = 0

        # normalize node_frags to a list of fragments for each node even
        # if there's only one fragment in the dataset provided.
        for i, frags in enumerate(node_frags):
            if isinstance(frags, dict):
                node_frags[i] = [frags]

        def get_response(req):
            if not node_map:
                _build_node_map(req)

            try:
                node_index = node_map[(req['ip'], req['port'])]
            except KeyError:
                raise Exception("Couldn't find node %s:%s in %r" % (
                    req['ip'], req['port'], all_nodes))
            try:
                frags = node_frags[node_index]
            except IndexError:
                raise Exception('Found node %r:%r at index %s - '
                                'but only got %s stub response nodes' % (
                                    req['ip'], req['port'], node_index,
                                    len(node_frags)))

            if not frags:
                return StubResponse(404)

            # determine response fragment (if any) for this call
            resp_frag = frags[call_count[node_index]]
            call_count[node_index] += 1
            frag_prefs = req['headers'].get('X-Backend-Fragment-Preferences')
            if not (frag_prefs or resp_frag.get('durable', True)):
                return StubResponse(404)

            # prepare durable timestamp and backend frags header for this node
            obj_stub = resp_frag['obj']
            ts2frags = defaultdict(list)
            durable_timestamp = None
            for frag in frags:
                ts_frag = frag['obj']['timestamp']
                if frag.get('durable', True):
                    durable_timestamp = ts_frag.internal
                ts2frags[ts_frag].append(frag['frag'])

            try:
                body = obj_stub['frags'][resp_frag['frag']]
            except IndexError as err:
                raise Exception(
                    'Frag index %s not defined: node index %s, frags %r\n%s' %
                    (resp_frag['frag'], node_index, [f['frag'] for f in frags],
                     err))
            headers = {
                'X-Object-Sysmeta-Ec-Content-Length': len(obj_stub['body']),
                'X-Object-Sysmeta-Ec-Etag': obj_stub['etag'],
                'X-Object-Sysmeta-Ec-Frag-Index': resp_frag['frag'],
                'X-Backend-Timestamp': obj_stub['timestamp'].internal,
                'X-Timestamp': obj_stub['timestamp'].normal,
                'X-Backend-Data-Timestamp': obj_stub['timestamp'].internal,
                'X-Backend-Fragments':
                    server._make_backend_fragments_header(ts2frags)
            }
            if durable_timestamp:
                headers['X-Backend-Durable-Timestamp'] = durable_timestamp

            return StubResponse(200, body, headers)

        return get_response

    def test_GET_with_frags_swapped_around(self):
        segment_size = self.policy.ec_segment_size
        test_data = ('test' * segment_size)[:-657]
        etag = md5(test_data).hexdigest()
        ec_archive_bodies = self._make_ec_archive_bodies(test_data)

        _part, primary_nodes = self.obj_ring.get_nodes('a', 'c', 'o')

        node_key = lambda n: (n['ip'], n['port'])

        ts = self._ts_iter.next()
        response_map = {
            node_key(n): StubResponse(200, ec_archive_bodies[i], {
                'X-Object-Sysmeta-Ec-Content-Length': len(test_data),
                'X-Object-Sysmeta-Ec-Etag': etag,
                'X-Object-Sysmeta-Ec-Frag-Index': i,
                'X-Timestamp': ts.normal,
                'X-Backend-Timestamp': ts.internal
            }) for i, n in enumerate(primary_nodes)
        }

        # swap a parity response into a data node
        data_node = random.choice(primary_nodes[:self.policy.ec_ndata])
        parity_node = random.choice(primary_nodes[self.policy.ec_ndata:])
        (response_map[node_key(data_node)],
         response_map[node_key(parity_node)]) = \
            (response_map[node_key(parity_node)],
             response_map[node_key(data_node)])

        def get_response(req):
            req_key = (req['ip'], req['port'])
            return response_map.pop(req_key)

        req = swob.Request.blank('/v1/a/c/o')
        with capture_http_requests(get_response) as log:
            resp = req.get_response(self.app)

        self.assertEqual(resp.status_int, 200)
        self.assertEqual(len(log), self.policy.ec_ndata)
        self.assertEqual(len(response_map),
                         len(primary_nodes) - self.policy.ec_ndata)

    def test_GET_with_no_success(self):
        node_frags = [[]] * 28  # no frags on any node

        fake_response = self._fake_ec_node_response(node_frags)

        req = swob.Request.blank('/v1/a/c/o')
        with capture_http_requests(fake_response) as log:
            resp = req.get_response(self.app)

        self.assertEqual(resp.status_int, 404)
        self.assertEqual(len(log), 2 * self.replicas())

    def test_GET_with_only_handoffs(self):
        obj1 = self._make_ec_object_stub()

        node_frags = [[]] * 14  # all primaries missing
        node_frags = node_frags + [  # handoffs
            {'obj': obj1, 'frag': 0},
            {'obj': obj1, 'frag': 1},
            {'obj': obj1, 'frag': 2},
            {'obj': obj1, 'frag': 3},
            {'obj': obj1, 'frag': 4},
            {'obj': obj1, 'frag': 5},
            {'obj': obj1, 'frag': 6},
            {'obj': obj1, 'frag': 7},
            {'obj': obj1, 'frag': 8},
            {'obj': obj1, 'frag': 9},
            {'obj': obj1, 'frag': 10},  # parity
            {'obj': obj1, 'frag': 11},  # parity
            {'obj': obj1, 'frag': 12},  # parity
            {'obj': obj1, 'frag': 13},  # parity
        ]

        fake_response = self._fake_ec_node_response(node_frags)

        req = swob.Request.blank('/v1/a/c/o')
        with capture_http_requests(fake_response) as log:
            resp = req.get_response(self.app)

        self.assertEqual(resp.status_int, 200)
        self.assertEqual(resp.headers['etag'], obj1['etag'])
        self.assertEqual(md5(resp.body).hexdigest(), obj1['etag'])

        collected_responses = defaultdict(list)
        for conn in log:
            etag = conn.resp.headers['X-Object-Sysmeta-Ec-Etag']
            index = conn.resp.headers['X-Object-Sysmeta-Ec-Frag-Index']
            collected_responses[etag].append(index)

        # GETS would be required to all primaries and then ndata handoffs
        self.assertEqual(len(log), self.replicas() + self.policy.ec_ndata)
        self.assertEqual(2, len(collected_responses))
        self.assertEqual(14, len(collected_responses[None]))  # 404s
        self.assertEqual(self.policy.ec_ndata,
                         len(collected_responses[obj1['etag']]))

    def test_GET_with_single_missed_overwrite_does_not_need_handoff(self):
        obj1 = self._make_ec_object_stub()
        obj2 = self._make_ec_object_stub()

        node_frags = [
            {'obj': obj2, 'frag': 0},
            {'obj': obj2, 'frag': 1},
            {'obj': obj1, 'frag': 2},  # missed over write
            {'obj': obj2, 'frag': 3},
            {'obj': obj2, 'frag': 4},
            {'obj': obj2, 'frag': 5},
            {'obj': obj2, 'frag': 6},
            {'obj': obj2, 'frag': 7},
            {'obj': obj2, 'frag': 8},
            {'obj': obj2, 'frag': 9},
            {'obj': obj2, 'frag': 10},  # parity
            {'obj': obj2, 'frag': 11},  # parity
            {'obj': obj2, 'frag': 12},  # parity
            {'obj': obj2, 'frag': 13},  # parity
            # {'obj': obj2, 'frag': 2},  # handoff (not used in this test)
        ]

        fake_response = self._fake_ec_node_response(node_frags)

        req = swob.Request.blank('/v1/a/c/o')
        with capture_http_requests(fake_response) as log:
            resp = req.get_response(self.app)

        self.assertEqual(resp.status_int, 200)
        self.assertEqual(resp.headers['etag'], obj2['etag'])
        self.assertEqual(md5(resp.body).hexdigest(), obj2['etag'])

        collected_responses = defaultdict(set)
        for conn in log:
            etag = conn.resp.headers['X-Object-Sysmeta-Ec-Etag']
            index = conn.resp.headers['X-Object-Sysmeta-Ec-Frag-Index']
            collected_responses[etag].add(index)

        # because the primary nodes are shuffled, it's possible the proxy
        # didn't even notice the missed overwrite frag - but it might have
        self.assertLessEqual(len(log), self.policy.ec_ndata + 1)
        self.assertLessEqual(len(collected_responses), 2)

        # ... regardless we should never need to fetch more than ec_ndata
        # frags for any given etag
        for etag, frags in collected_responses.items():
            self.assertLessEqual(len(frags), self.policy.ec_ndata,
                                 'collected %s frags for etag %s' % (
                                     len(frags), etag))

    def test_GET_with_many_missed_overwrite_will_need_handoff(self):
        obj1 = self._make_ec_object_stub()
        obj2 = self._make_ec_object_stub()

        node_frags = [
            {'obj': obj2, 'frag': 0},
            {'obj': obj2, 'frag': 1},
            {'obj': obj1, 'frag': 2},  # missed
            {'obj': obj2, 'frag': 3},
            {'obj': obj2, 'frag': 4},
            {'obj': obj2, 'frag': 5},
            {'obj': obj1, 'frag': 6},  # missed
            {'obj': obj2, 'frag': 7},
            {'obj': obj2, 'frag': 8},
            {'obj': obj1, 'frag': 9},  # missed
            {'obj': obj1, 'frag': 10},  # missed
            {'obj': obj1, 'frag': 11},  # missed
            {'obj': obj2, 'frag': 12},
            {'obj': obj2, 'frag': 13},
            {'obj': obj2, 'frag': 6},  # handoff
        ]

        fake_response = self._fake_ec_node_response(node_frags)

        req = swob.Request.blank('/v1/a/c/o')
        with capture_http_requests(fake_response) as log:
            resp = req.get_response(self.app)

        self.assertEqual(resp.status_int, 200)
        self.assertEqual(resp.headers['etag'], obj2['etag'])
        self.assertEqual(md5(resp.body).hexdigest(), obj2['etag'])

        collected_responses = defaultdict(set)
        for conn in log:
            etag = conn.resp.headers['X-Object-Sysmeta-Ec-Etag']
            index = conn.resp.headers['X-Object-Sysmeta-Ec-Frag-Index']
            collected_responses[etag].add(index)

        # there's not enough of the obj2 etag on the primaries, we would
        # have collected responses for both etags, and would have made
        # one more request to the handoff node
        self.assertEqual(len(log), self.replicas() + 1)
        self.assertEqual(len(collected_responses), 2)

        # ... regardless we should never need to fetch more than ec_ndata
        # frags for any given etag
        for etag, frags in collected_responses.items():
            self.assertLessEqual(len(frags), self.policy.ec_ndata,
                                 'collected %s frags for etag %s' % (
                                     len(frags), etag))

    def test_GET_with_missing_and_mixed_frags_will_dig_deep_but_succeed(self):
        obj1 = self._make_ec_object_stub(timestamp=self.ts())
        obj2 = self._make_ec_object_stub(timestamp=self.ts())

        node_frags = [
            {'obj': obj1, 'frag': 0},
            {'obj': obj2, 'frag': 0},
            [],
            {'obj': obj1, 'frag': 1},
            {'obj': obj2, 'frag': 1},
            [],
            {'obj': obj1, 'frag': 2},
            {'obj': obj2, 'frag': 2},
            [],
            {'obj': obj1, 'frag': 3},
            {'obj': obj2, 'frag': 3},
            [],
            {'obj': obj1, 'frag': 4},
            {'obj': obj2, 'frag': 4},
            [],
            {'obj': obj1, 'frag': 5},
            {'obj': obj2, 'frag': 5},
            [],
            {'obj': obj1, 'frag': 6},
            {'obj': obj2, 'frag': 6},
            [],
            {'obj': obj1, 'frag': 7},
            {'obj': obj2, 'frag': 7},
            [],
            {'obj': obj1, 'frag': 8},
            {'obj': obj2, 'frag': 8},
            [],
            {'obj': obj2, 'frag': 9},
        ]

        fake_response = self._fake_ec_node_response(node_frags)

        req = swob.Request.blank('/v1/a/c/o')
        with capture_http_requests(fake_response) as log:
            resp = req.get_response(self.app)

        self.assertEqual(resp.status_int, 200)
        self.assertEqual(resp.headers['etag'], obj2['etag'])
        self.assertEqual(md5(resp.body).hexdigest(), obj2['etag'])

        collected_responses = defaultdict(set)
        for conn in log:
            etag = conn.resp.headers['X-Object-Sysmeta-Ec-Etag']
            index = conn.resp.headers['X-Object-Sysmeta-Ec-Frag-Index']
            collected_responses[etag].add(index)

        # we go exactly as long as we have to, finding two different
        # etags and some 404's (i.e. collected_responses[None])
        self.assertEqual(len(log), len(node_frags))
        self.assertEqual(len(collected_responses), 3)

        # ... regardless we should never need to fetch more than ec_ndata
        # frags for any given etag
        for etag, frags in collected_responses.items():
            self.assertLessEqual(len(frags), self.policy.ec_ndata,
                                 'collected %s frags for etag %s' % (
                                     len(frags), etag))

    def test_GET_with_missing_and_mixed_frags_will_dig_deep_but_stop(self):
        obj1 = self._make_ec_object_stub()
        obj2 = self._make_ec_object_stub()

        node_frags = [
            {'obj': obj1, 'frag': 0},
            {'obj': obj2, 'frag': 0},
            [],
            {'obj': obj1, 'frag': 1},
            {'obj': obj2, 'frag': 1},
            [],
            {'obj': obj1, 'frag': 2},
            {'obj': obj2, 'frag': 2},
            [],
            {'obj': obj1, 'frag': 3},
            {'obj': obj2, 'frag': 3},
            [],
            {'obj': obj1, 'frag': 4},
            {'obj': obj2, 'frag': 4},
            [],
            {'obj': obj1, 'frag': 5},
            {'obj': obj2, 'frag': 5},
            [],
            {'obj': obj1, 'frag': 6},
            {'obj': obj2, 'frag': 6},
            [],
            {'obj': obj1, 'frag': 7},
            {'obj': obj2, 'frag': 7},
            [],
            {'obj': obj1, 'frag': 8},
            {'obj': obj2, 'frag': 8},
            [],
            # handoffs are iter'd in order so proxy will see 404 from this
            # final handoff
            [],
        ]

        fake_response = self._fake_ec_node_response(node_frags)

        req = swob.Request.blank('/v1/a/c/o')
        with capture_http_requests(fake_response) as log:
            resp = req.get_response(self.app)

        self.assertEqual(resp.status_int, 404)

        collected_responses = defaultdict(set)
        for conn in log:
            etag = conn.resp.headers['X-Object-Sysmeta-Ec-Etag']
            index = conn.resp.headers['X-Object-Sysmeta-Ec-Frag-Index']
            collected_responses[etag].add(index)

        # default node_iter will exhaust at 2 * replicas
        self.assertEqual(len(log), 2 * self.replicas())
        self.assertEqual(len(collected_responses), 3)

        # ... regardless we should never need to fetch more than ec_ndata
        # frags for any given etag
        for etag, frags in collected_responses.items():
            self.assertLessEqual(len(frags), self.policy.ec_ndata,
                                 'collected %s frags for etag %s' % (
                                     len(frags), etag))

    def test_GET_with_duplicate_but_sufficient_frag_indexes(self):
        obj1 = self._make_ec_object_stub()
        # proxy should ignore duplicated frag indexes and continue search for
        # a set of unique indexes, finding last one on a handoff
        node_frags = [
            {'obj': obj1, 'frag': 0},
            {'obj': obj1, 'frag': 0},  # duplicate frag
            {'obj': obj1, 'frag': 1},
            {'obj': obj1, 'frag': 1},  # duplicate frag
            {'obj': obj1, 'frag': 2},
            {'obj': obj1, 'frag': 2},  # duplicate frag
            {'obj': obj1, 'frag': 3},
            {'obj': obj1, 'frag': 3},  # duplicate frag
            {'obj': obj1, 'frag': 4},
            {'obj': obj1, 'frag': 4},  # duplicate frag
            {'obj': obj1, 'frag': 10},
            {'obj': obj1, 'frag': 11},
            {'obj': obj1, 'frag': 12},
            {'obj': obj1, 'frag': 13},
            {'obj': obj1, 'frag': 5},  # handoff
        ]

        fake_response = self._fake_ec_node_response(node_frags)

        req = swob.Request.blank('/v1/a/c/o')
        with capture_http_requests(fake_response) as log:
            resp = req.get_response(self.app)

        self.assertEqual(resp.status_int, 200)
        self.assertEqual(resp.headers['etag'], obj1['etag'])
        self.assertEqual(md5(resp.body).hexdigest(), obj1['etag'])

        # expect a request to all primaries plus one handoff
        self.assertEqual(self.replicas() + 1, len(log))
        collected_indexes = defaultdict(list)
        for conn in log:
            fi = conn.resp.headers.get('X-Object-Sysmeta-Ec-Frag-Index')
            if fi is not None:
                collected_indexes[fi].append(conn)
        self.assertEqual(len(collected_indexes), self.policy.ec_ndata)

    def test_GET_with_duplicate_and_hidden_frag_indexes(self):
        obj1 = self._make_ec_object_stub()
        # proxy should ignore duplicated frag indexes and continue search for
        # a set of unique indexes, finding last one on a handoff
        node_frags = [
            [{'obj': obj1, 'frag': 0}, {'obj': obj1, 'frag': 5}],
            {'obj': obj1, 'frag': 0},  # duplicate frag
            {'obj': obj1, 'frag': 1},
            {'obj': obj1, 'frag': 1},  # duplicate frag
            {'obj': obj1, 'frag': 2},
            {'obj': obj1, 'frag': 2},  # duplicate frag
            {'obj': obj1, 'frag': 3},
            {'obj': obj1, 'frag': 3},  # duplicate frag
            {'obj': obj1, 'frag': 4},
            {'obj': obj1, 'frag': 4},  # duplicate frag
            {'obj': obj1, 'frag': 10},
            {'obj': obj1, 'frag': 11},
            {'obj': obj1, 'frag': 12},
            {'obj': obj1, 'frag': 13},
        ]

        fake_response = self._fake_ec_node_response(node_frags)

        req = swob.Request.blank('/v1/a/c/o')
        with capture_http_requests(fake_response) as log:
            resp = req.get_response(self.app)

        self.assertEqual(resp.status_int, 200)
        self.assertEqual(resp.headers['etag'], obj1['etag'])
        self.assertEqual(md5(resp.body).hexdigest(), obj1['etag'])

        # Expect a maximum of one request to each primary plus one extra
        # request to node 1. Actual value could be less if the extra request
        # occurs and quorum is reached before requests to nodes with a
        # duplicate frag.
        self.assertLessEqual(len(log), self.replicas() + 1)
        collected_indexes = defaultdict(list)
        for conn in log:
            fi = conn.resp.headers.get('X-Object-Sysmeta-Ec-Frag-Index')
            if fi is not None:
                collected_indexes[fi].append(conn)
        self.assertEqual(len(collected_indexes), self.policy.ec_ndata)

    def test_GET_with_duplicate_but_insufficient_frag(self):
        obj1 = self._make_ec_object_stub()
        # proxy should ignore duplicated frag indexes and continue search for
        # a set of unique indexes, but fails to find one
        node_frags = [
            {'obj': obj1, 'frag': 0},
            {'obj': obj1, 'frag': 0},  # duplicate frag
            {'obj': obj1, 'frag': 1},
            {'obj': obj1, 'frag': 1},  # duplicate frag
            {'obj': obj1, 'frag': 2},
            {'obj': obj1, 'frag': 2},  # duplicate frag
            {'obj': obj1, 'frag': 3},
            {'obj': obj1, 'frag': 3},  # duplicate frag
            {'obj': obj1, 'frag': 4},
            {'obj': obj1, 'frag': 4},  # duplicate frag
            {'obj': obj1, 'frag': 10},
            {'obj': obj1, 'frag': 11},
            {'obj': obj1, 'frag': 12},
            {'obj': obj1, 'frag': 13},
        ] + [[]] * 14  # 404 from handoffs

        fake_response = self._fake_ec_node_response(node_frags)

        req = swob.Request.blank('/v1/a/c/o')
        with capture_http_requests(fake_response) as log:
            resp = req.get_response(self.app)

        self.assertEqual(resp.status_int, 404)

        # expect a request to all nodes
        self.assertEqual(2 * self.replicas(), len(log))
        collected_indexes = defaultdict(list)
        collected_etags = set()
        for conn in log:
            etag = conn.resp.headers['X-Object-Sysmeta-Ec-Etag']
            collected_etags.add(etag)  # will be None from handoffs
            fi = conn.resp.headers.get('X-Object-Sysmeta-Ec-Frag-Index')
            if fi is not None:
                collected_indexes[fi].append(conn)
        self.assertEqual(len(collected_indexes), self.policy.ec_ndata - 1)
        self.assertEqual({obj1['etag'], None}, collected_etags)

    def test_GET_with_missing_and_mixed_frags_may_503(self):
        obj1 = self._make_ec_object_stub()
        obj2 = self._make_ec_object_stub()
        # we get a 503 when all the handoffs return 200
        node_frags = [[]] * 14  # primaries have no frags
        node_frags = node_frags + [  # handoffs all have frags
            {'obj': obj1, 'frag': 0},
            {'obj': obj2, 'frag': 0},
            {'obj': obj1, 'frag': 1},
            {'obj': obj2, 'frag': 1},
            {'obj': obj1, 'frag': 2},
            {'obj': obj2, 'frag': 2},
            {'obj': obj1, 'frag': 3},
            {'obj': obj2, 'frag': 3},
            {'obj': obj1, 'frag': 4},
            {'obj': obj2, 'frag': 4},
            {'obj': obj1, 'frag': 5},
            {'obj': obj2, 'frag': 5},
            {'obj': obj1, 'frag': 6},
            {'obj': obj2, 'frag': 6},
        ]
        fake_response = self._fake_ec_node_response(node_frags)

        req = swob.Request.blank('/v1/a/c/o')
        with capture_http_requests(fake_response) as log:
            resp = req.get_response(self.app)

        self.assertEqual(resp.status_int, 503)
        # never get a quorum so all nodes are searched
        self.assertEqual(len(log), 2 * self.replicas())
        collected_indexes = defaultdict(list)
        for conn in log:
            fi = conn.resp.headers.get('X-Object-Sysmeta-Ec-Frag-Index')
            if fi is not None:
                collected_indexes[fi].append(conn)
        self.assertEqual(len(collected_indexes), 7)

    def test_GET_with_mixed_frags_and_no_quorum_will_503(self):
        # all nodes have a frag but there is no one set that reaches quorum,
        # which means there is no backend 404 response, but proxy should still
        # return 404 rather than 503
        obj1 = self._make_ec_object_stub()
        obj2 = self._make_ec_object_stub()
        obj3 = self._make_ec_object_stub()
        obj4 = self._make_ec_object_stub()

        node_frags = [
            {'obj': obj1, 'frag': 0},
            {'obj': obj2, 'frag': 0},
            {'obj': obj3, 'frag': 0},
            {'obj': obj1, 'frag': 1},
            {'obj': obj2, 'frag': 1},
            {'obj': obj3, 'frag': 1},
            {'obj': obj1, 'frag': 2},
            {'obj': obj2, 'frag': 2},
            {'obj': obj3, 'frag': 2},
            {'obj': obj1, 'frag': 3},
            {'obj': obj2, 'frag': 3},
            {'obj': obj3, 'frag': 3},
            {'obj': obj1, 'frag': 4},
            {'obj': obj2, 'frag': 4},
            {'obj': obj3, 'frag': 4},
            {'obj': obj1, 'frag': 5},
            {'obj': obj2, 'frag': 5},
            {'obj': obj3, 'frag': 5},
            {'obj': obj1, 'frag': 6},
            {'obj': obj2, 'frag': 6},
            {'obj': obj3, 'frag': 6},
            {'obj': obj1, 'frag': 7},
            {'obj': obj2, 'frag': 7},
            {'obj': obj3, 'frag': 7},
            {'obj': obj1, 'frag': 8},
            {'obj': obj2, 'frag': 8},
            {'obj': obj3, 'frag': 8},
            {'obj': obj4, 'frag': 8},
        ]

        fake_response = self._fake_ec_node_response(node_frags)

        req = swob.Request.blank('/v1/a/c/o')
        with capture_http_requests(fake_response) as log:
            resp = req.get_response(self.app)

        self.assertEqual(resp.status_int, 503)

        collected_etags = set()
        collected_status = set()
        for conn in log:
            etag = conn.resp.headers['X-Object-Sysmeta-Ec-Etag']
            collected_etags.add(etag)
            collected_status.add(conn.resp.status)

        # default node_iter will exhaust at 2 * replicas
        self.assertEqual(len(log), 2 * self.replicas())
        self.assertEqual(
            {obj1['etag'], obj2['etag'], obj3['etag'], obj4['etag']},
            collected_etags)
        self.assertEqual({200}, collected_status)

    def test_GET_with_quorum_durable_files(self):
        # verify that only (ec_nparity + 1) nodes need to be durable for a GET
        # to be completed with ec_ndata requests.
        obj1 = self._make_ec_object_stub()

        node_frags = [
            {'obj': obj1, 'frag': 0, 'durable': True},  # durable
            {'obj': obj1, 'frag': 1, 'durable': True},  # durable
            {'obj': obj1, 'frag': 2, 'durable': True},  # durable
            {'obj': obj1, 'frag': 3, 'durable': True},  # durable
            {'obj': obj1, 'frag': 4, 'durable': True},  # durable
            {'obj': obj1, 'frag': 5, 'durable': False},
            {'obj': obj1, 'frag': 6, 'durable': False},
            {'obj': obj1, 'frag': 7, 'durable': False},
            {'obj': obj1, 'frag': 8, 'durable': False},
            {'obj': obj1, 'frag': 9, 'durable': False},
            {'obj': obj1, 'frag': 10, 'durable': False},  # parity
            {'obj': obj1, 'frag': 11, 'durable': False},  # parity
            {'obj': obj1, 'frag': 12, 'durable': False},  # parity
            {'obj': obj1, 'frag': 13, 'durable': False},  # parity
        ]  # handoffs not used in this scenario

        fake_response = self._fake_ec_node_response(list(node_frags))

        req = swob.Request.blank('/v1/a/c/o')
        with capture_http_requests(fake_response) as log:
            resp = req.get_response(self.app)

        self.assertEqual(resp.status_int, 200)
        self.assertEqual(resp.headers['etag'], obj1['etag'])
        self.assertEqual(md5(resp.body).hexdigest(), obj1['etag'])

        self.assertEqual(self.policy.ec_ndata, len(log))
        collected_durables = []
        for conn in log:
            if (conn.resp.headers.get('X-Backend-Durable-Timestamp')
                    == conn.resp.headers.get('X-Backend-Data-Timestamp')):
                collected_durables.append(conn)
        # because nodes are shuffled we can't be sure how many durables are
        # returned but it must be at least 1 and cannot exceed 5
        self.assertLessEqual(len(collected_durables), 5)
        self.assertGreaterEqual(len(collected_durables), 1)

    def test_GET_with_single_durable_file(self):
        # verify that a single durable is sufficient for a GET
        # to be completed with ec_ndata requests.
        obj1 = self._make_ec_object_stub()

        node_frags = [
            {'obj': obj1, 'frag': 0, 'durable': True},  # durable
            {'obj': obj1, 'frag': 1, 'durable': False},
            {'obj': obj1, 'frag': 2, 'durable': False},
            {'obj': obj1, 'frag': 3, 'durable': False},
            {'obj': obj1, 'frag': 4, 'durable': False},
            {'obj': obj1, 'frag': 5, 'durable': False},
            {'obj': obj1, 'frag': 6, 'durable': False},
            {'obj': obj1, 'frag': 7, 'durable': False},
            {'obj': obj1, 'frag': 8, 'durable': False},
            {'obj': obj1, 'frag': 9, 'durable': False},
            {'obj': obj1, 'frag': 10, 'durable': False},  # parity
            {'obj': obj1, 'frag': 11, 'durable': False},  # parity
            {'obj': obj1, 'frag': 12, 'durable': False},  # parity
            {'obj': obj1, 'frag': 13, 'durable': False},  # parity
        ]  # handoffs not used in this scenario

        fake_response = self._fake_ec_node_response(list(node_frags))

        req = swob.Request.blank('/v1/a/c/o')
        with capture_http_requests(fake_response) as log:
            resp = req.get_response(self.app)

        self.assertEqual(resp.status_int, 200)
        self.assertEqual(resp.headers['etag'], obj1['etag'])
        self.assertEqual(md5(resp.body).hexdigest(), obj1['etag'])

        collected_durables = []
        for conn in log:
            if (conn.resp.headers.get('X-Backend-Durable-Timestamp')
                    == conn.resp.headers.get('X-Backend-Data-Timestamp')):
                collected_durables.append(conn)
        # because nodes are shuffled we can't be sure how many non-durables
        # are returned before the durable, but we do expect a single durable
        self.assertEqual(1, len(collected_durables))

    def test_GET_with_no_durable_files(self):
        # verify that at least one durable is necessary for a successful GET
        obj1 = self._make_ec_object_stub()
        node_frags = [
            {'obj': obj1, 'frag': 0, 'durable': False},
            {'obj': obj1, 'frag': 1, 'durable': False},
            {'obj': obj1, 'frag': 2, 'durable': False},
            {'obj': obj1, 'frag': 3, 'durable': False},
            {'obj': obj1, 'frag': 4, 'durable': False},
            {'obj': obj1, 'frag': 5, 'durable': False},
            {'obj': obj1, 'frag': 6, 'durable': False},
            {'obj': obj1, 'frag': 7, 'durable': False},
            {'obj': obj1, 'frag': 8, 'durable': False},
            {'obj': obj1, 'frag': 9, 'durable': False},
            {'obj': obj1, 'frag': 10, 'durable': False},  # parity
            {'obj': obj1, 'frag': 11, 'durable': False},  # parity
            {'obj': obj1, 'frag': 12, 'durable': False},  # parity
            {'obj': obj1, 'frag': 13, 'durable': False},  # parity
        ] + [[]] * self.replicas()  # handoffs

        fake_response = self._fake_ec_node_response(list(node_frags))

        req = swob.Request.blank('/v1/a/c/o')
        with capture_http_requests(fake_response) as log:
            resp = req.get_response(self.app)

        self.assertEqual(resp.status_int, 404)
        # all 28 nodes tried with an optimistic get, none are durable and none
        # report having a durable timestamp
        self.assertEqual(28, len(log))

    def test_GET_with_missing_durable_files_and_mixed_etags(self):
        obj1 = self._make_ec_object_stub()
        obj2 = self._make_ec_object_stub()

        # non-quorate durables for another object won't stop us finding the
        # quorate object
        node_frags = [
            # ec_ndata - 1 frags of obj2 are available and durable
            {'obj': obj2, 'frag': 0, 'durable': True},
            {'obj': obj2, 'frag': 1, 'durable': True},
            {'obj': obj2, 'frag': 2, 'durable': True},
            {'obj': obj2, 'frag': 3, 'durable': True},
            {'obj': obj2, 'frag': 4, 'durable': True},
            {'obj': obj2, 'frag': 5, 'durable': True},
            {'obj': obj2, 'frag': 6, 'durable': True},
            {'obj': obj2, 'frag': 7, 'durable': True},
            {'obj': obj2, 'frag': 8, 'durable': True},
            # ec_ndata frags of obj1 are available and one is durable
            {'obj': obj1, 'frag': 0, 'durable': False},
            {'obj': obj1, 'frag': 1, 'durable': False},
            {'obj': obj1, 'frag': 2, 'durable': False},
            {'obj': obj1, 'frag': 3, 'durable': False},
            {'obj': obj1, 'frag': 4, 'durable': False},
            {'obj': obj1, 'frag': 5, 'durable': False},
            {'obj': obj1, 'frag': 6, 'durable': False},
            {'obj': obj1, 'frag': 7, 'durable': False},
            {'obj': obj1, 'frag': 8, 'durable': False},
            {'obj': obj1, 'frag': 9, 'durable': True},
        ]

        fake_response = self._fake_ec_node_response(list(node_frags))

        req = swob.Request.blank('/v1/a/c/o')
        with capture_http_requests(fake_response):
            resp = req.get_response(self.app)

        self.assertEqual(resp.status_int, 200)
        self.assertEqual(resp.headers['etag'], obj1['etag'])
        self.assertEqual(md5(resp.body).hexdigest(), obj1['etag'])

        # Quorum of non-durables for a different object won't
        # prevent us hunting down the durable object
        node_frags = [
            # primaries
            {'obj': obj2, 'frag': 0, 'durable': False},
            {'obj': obj2, 'frag': 1, 'durable': False},
            {'obj': obj2, 'frag': 2, 'durable': False},
            {'obj': obj2, 'frag': 3, 'durable': False},
            {'obj': obj2, 'frag': 4, 'durable': False},
            {'obj': obj2, 'frag': 5, 'durable': False},
            {'obj': obj2, 'frag': 6, 'durable': False},
            {'obj': obj2, 'frag': 7, 'durable': False},
            {'obj': obj2, 'frag': 8, 'durable': False},
            {'obj': obj2, 'frag': 9, 'durable': False},
            {'obj': obj2, 'frag': 10, 'durable': False},
            {'obj': obj2, 'frag': 11, 'durable': False},
            {'obj': obj2, 'frag': 12, 'durable': False},
            {'obj': obj2, 'frag': 13, 'durable': False},
            # handoffs
            {'obj': obj1, 'frag': 0, 'durable': False},
            {'obj': obj1, 'frag': 1, 'durable': False},
            {'obj': obj1, 'frag': 2, 'durable': False},
            {'obj': obj1, 'frag': 3, 'durable': False},
            {'obj': obj1, 'frag': 4, 'durable': False},
            {'obj': obj1, 'frag': 5, 'durable': False},
            {'obj': obj1, 'frag': 6, 'durable': False},
            {'obj': obj1, 'frag': 7, 'durable': False},
            {'obj': obj1, 'frag': 8, 'durable': False},
            {'obj': obj1, 'frag': 9, 'durable': False},
            {'obj': obj1, 'frag': 10, 'durable': False},  # parity
            {'obj': obj1, 'frag': 11, 'durable': False},  # parity
            {'obj': obj1, 'frag': 12, 'durable': False},  # parity
            {'obj': obj1, 'frag': 13, 'durable': True},  # parity
        ]

        fake_response = self._fake_ec_node_response(list(node_frags))

        req = swob.Request.blank('/v1/a/c/o')
        with capture_http_requests(fake_response):
            resp = req.get_response(self.app)

        self.assertEqual(resp.status_int, 200)
        self.assertEqual(resp.headers['etag'], obj1['etag'])
        self.assertEqual(md5(resp.body).hexdigest(), obj1['etag'])

    def test_GET_with_missing_durables_and_older_durables(self):
        # scenario: non-durable frags of newer obj1 obscure all durable frags
        # of older obj2, so first 14 requests result in a non-durable set.
        # At that point (or before) the proxy knows that a durable set of
        # frags for obj2 exists so will fetch them, requiring another 10
        # directed requests.
        obj2 = self._make_ec_object_stub(timestamp=self._ts_iter.next())
        obj1 = self._make_ec_object_stub(timestamp=self._ts_iter.next())

        node_frags = [
            [{'obj': obj1, 'frag': 0, 'durable': False}],  # obj2 missing
            [{'obj': obj1, 'frag': 1, 'durable': False},
             {'obj': obj2, 'frag': 1, 'durable': True}],
            [{'obj': obj1, 'frag': 2, 'durable': False}],  # obj2 missing
            [{'obj': obj1, 'frag': 3, 'durable': False},
             {'obj': obj2, 'frag': 3, 'durable': True}],
            [{'obj': obj1, 'frag': 4, 'durable': False},
             {'obj': obj2, 'frag': 4, 'durable': True}],
            [{'obj': obj1, 'frag': 5, 'durable': False},
             {'obj': obj2, 'frag': 5, 'durable': True}],
            [{'obj': obj1, 'frag': 6, 'durable': False},
             {'obj': obj2, 'frag': 6, 'durable': True}],
            [{'obj': obj1, 'frag': 7, 'durable': False},
             {'obj': obj2, 'frag': 7, 'durable': True}],
            [{'obj': obj1, 'frag': 8, 'durable': False},
             {'obj': obj2, 'frag': 8, 'durable': True}],
            [{'obj': obj1, 'frag': 9, 'durable': False}],  # obj2 missing
            [{'obj': obj1, 'frag': 10, 'durable': False},
             {'obj': obj2, 'frag': 10, 'durable': True}],
            [{'obj': obj1, 'frag': 11, 'durable': False},
             {'obj': obj2, 'frag': 11, 'durable': True}],
            [{'obj': obj1, 'frag': 12, 'durable': False}],  # obj2 missing
            [{'obj': obj1, 'frag': 13, 'durable': False},
             {'obj': obj2, 'frag': 13, 'durable': True}],
        ]

        fake_response = self._fake_ec_node_response(list(node_frags))

        req = swob.Request.blank('/v1/a/c/o')
        with capture_http_requests(fake_response) as log:
            resp = req.get_response(self.app)

        self.assertEqual(resp.status_int, 200)
        self.assertEqual(resp.headers['etag'], obj2['etag'])
        self.assertEqual(md5(resp.body).hexdigest(), obj2['etag'])
        # max: proxy will GET all non-durable obj1 frags and then 10 obj frags
        self.assertLessEqual(len(log), self.replicas() + self.policy.ec_ndata)
        # min: proxy will GET 10 non-durable obj1 frags and then 10 obj frags
        self.assertGreaterEqual(len(log), 2 * self.policy.ec_ndata)

        # scenario: obj3 has 14 frags but only 2 are durable and these are
        # obscured by two non-durable frags of obj1. There is also a single
        # non-durable frag of obj2. The proxy will need to do at least 10
        # GETs to see all the obj3 frags plus 1 more to GET a durable frag.
        # The proxy may also do one more GET if the obj2 frag is found.
        obj2 = self._make_ec_object_stub(timestamp=self._ts_iter.next())
        obj3 = self._make_ec_object_stub(timestamp=self._ts_iter.next())
        obj1 = self._make_ec_object_stub(timestamp=self._ts_iter.next())

        node_frags = [
            [{'obj': obj1, 'frag': 0, 'durable': False},  # obj1 frag
             {'obj': obj3, 'frag': 0, 'durable': True}],
            [{'obj': obj1, 'frag': 1, 'durable': False},  # obj1 frag
             {'obj': obj3, 'frag': 1, 'durable': True}],
            [{'obj': obj2, 'frag': 2, 'durable': False},  # obj2 frag
             {'obj': obj3, 'frag': 2, 'durable': False}],
            [{'obj': obj3, 'frag': 3, 'durable': False}],
            [{'obj': obj3, 'frag': 4, 'durable': False}],
            [{'obj': obj3, 'frag': 5, 'durable': False}],
            [{'obj': obj3, 'frag': 6, 'durable': False}],
            [{'obj': obj3, 'frag': 7, 'durable': False}],
            [{'obj': obj3, 'frag': 8, 'durable': False}],
            [{'obj': obj3, 'frag': 9, 'durable': False}],
            [{'obj': obj3, 'frag': 10, 'durable': False}],
            [{'obj': obj3, 'frag': 11, 'durable': False}],
            [{'obj': obj3, 'frag': 12, 'durable': False}],
            [{'obj': obj3, 'frag': 13, 'durable': False}],
        ]

        fake_response = self._fake_ec_node_response(list(node_frags))

        req = swob.Request.blank('/v1/a/c/o')
        with capture_http_requests(fake_response) as log:
            resp = req.get_response(self.app)

        self.assertEqual(resp.status_int, 200)
        self.assertEqual(resp.headers['etag'], obj3['etag'])
        self.assertEqual(md5(resp.body).hexdigest(), obj3['etag'])
        self.assertGreaterEqual(len(log), self.policy.ec_ndata + 1)
        self.assertLessEqual(len(log), self.policy.ec_ndata + 3)

    def test_GET_with_missing_durables_and_older_non_durables(self):
        # scenario: non-durable frags of newer obj1 obscure all frags
        # of older obj2, so first 28 requests result in a non-durable set.
        # There are only 10 frags for obj2 and one is not durable.
        obj2 = self._make_ec_object_stub(timestamp=self._ts_iter.next())
        obj1 = self._make_ec_object_stub(timestamp=self._ts_iter.next())

        node_frags = [
            [{'obj': obj1, 'frag': 0, 'durable': False}],  # obj2 missing
            [{'obj': obj1, 'frag': 1, 'durable': False},
             {'obj': obj2, 'frag': 1, 'durable': False}],  # obj2 non-durable
            [{'obj': obj1, 'frag': 2, 'durable': False}],  # obj2 missing
            [{'obj': obj1, 'frag': 3, 'durable': False},
             {'obj': obj2, 'frag': 3, 'durable': True}],
            [{'obj': obj1, 'frag': 4, 'durable': False},
             {'obj': obj2, 'frag': 4, 'durable': True}],
            [{'obj': obj1, 'frag': 5, 'durable': False},
             {'obj': obj2, 'frag': 5, 'durable': True}],
            [{'obj': obj1, 'frag': 6, 'durable': False},
             {'obj': obj2, 'frag': 6, 'durable': True}],
            [{'obj': obj1, 'frag': 7, 'durable': False},
             {'obj': obj2, 'frag': 7, 'durable': True}],
            [{'obj': obj1, 'frag': 8, 'durable': False},
             {'obj': obj2, 'frag': 8, 'durable': True}],
            [{'obj': obj1, 'frag': 9, 'durable': False}],  # obj2 missing
            [{'obj': obj1, 'frag': 10, 'durable': False},
             {'obj': obj2, 'frag': 10, 'durable': True}],
            [{'obj': obj1, 'frag': 11, 'durable': False},
             {'obj': obj2, 'frag': 11, 'durable': True}],
            [{'obj': obj1, 'frag': 12, 'durable': False}],  # obj2 missing
            [{'obj': obj1, 'frag': 13, 'durable': False},
             {'obj': obj2, 'frag': 13, 'durable': True}],
            [],                                             # 1 empty primary
        ]

        fake_response = self._fake_ec_node_response(list(node_frags))

        req = swob.Request.blank('/v1/a/c/o')
        with capture_http_requests(fake_response) as log:
            resp = req.get_response(self.app)

        self.assertEqual(resp.status_int, 200)
        self.assertEqual(resp.headers['etag'], obj2['etag'])
        self.assertEqual(md5(resp.body).hexdigest(), obj2['etag'])
        # max: proxy will GET all non-durable obj1 frags and then 10 obj2 frags
        self.assertLessEqual(len(log), self.replicas() + self.policy.ec_ndata)
        # min: proxy will GET 10 non-durable obj1 frags and then 10 obj2 frags
        self.assertGreaterEqual(len(log), 2 * self.policy.ec_ndata)

    def test_GET_with_mixed_etags_at_same_timestamp(self):
        # this scenario should never occur but if there are somehow
        # fragments for different content at the same timestamp then the
        # object controller should handle it gracefully
        ts = self.ts()  # force equal timestamps for two objects
        obj1 = self._make_ec_object_stub(timestamp=ts)
        obj2 = self._make_ec_object_stub(timestamp=ts)

        node_frags = [
            # 7 frags of obj2 are available and durable
            {'obj': obj2, 'frag': 0, 'durable': True},
            {'obj': obj2, 'frag': 1, 'durable': True},
            {'obj': obj2, 'frag': 2, 'durable': True},
            {'obj': obj2, 'frag': 3, 'durable': True},
            {'obj': obj2, 'frag': 4, 'durable': True},
            {'obj': obj2, 'frag': 5, 'durable': True},
            {'obj': obj2, 'frag': 6, 'durable': True},
            # 7 frags of obj1 are available and durable
            {'obj': obj1, 'frag': 7, 'durable': True},
            {'obj': obj1, 'frag': 8, 'durable': True},
            {'obj': obj1, 'frag': 9, 'durable': True},
            {'obj': obj1, 'frag': 10, 'durable': True},
            {'obj': obj1, 'frag': 11, 'durable': True},
            {'obj': obj1, 'frag': 12, 'durable': True},
            {'obj': obj1, 'frag': 13, 'durable': True},
        ] + [[]] * self.replicas()  # handoffs

        fake_response = self._fake_ec_node_response(list(node_frags))

        req = swob.Request.blank('/v1/a/c/o')
        with capture_http_requests(fake_response) as log:
            resp = req.get_response(self.app)
        # read body to provoke any EC decode errors
        self.assertFalse(resp.body)

        self.assertEqual(resp.status_int, 404)
        self.assertEqual(len(log), self.replicas() * 2)
        collected_etags = set()
        for conn in log:
            etag = conn.resp.headers['X-Object-Sysmeta-Ec-Etag']
            collected_etags.add(etag)  # will be None from handoffs
        self.assertEqual({obj1['etag'], obj2['etag'], None}, collected_etags)
        log_lines = self.app.logger.get_lines_for_level('error')
        self.assertEqual(log_lines,
                         ['Problem with fragment response: ETag mismatch'] * 7)

    def test_GET_mixed_success_with_range(self):
        fragment_size = self.policy.fragment_size

        ec_stub = self._make_ec_object_stub()
        frag_archives = ec_stub['frags']
        frag_archive_size = len(ec_stub['frags'][0])

        headers = {
            'Content-Type': 'text/plain',
            'Content-Length': fragment_size,
            'Content-Range': 'bytes 0-%s/%s' % (fragment_size - 1,
                                                frag_archive_size),
            'X-Object-Sysmeta-Ec-Content-Length': len(ec_stub['body']),
            'X-Object-Sysmeta-Ec-Etag': ec_stub['etag'],
            'X-Timestamp': Timestamp(self._ts_iter.next()).normal,
        }
        responses = [
            StubResponse(206, frag_archives[0][:fragment_size], headers, 0),
            StubResponse(206, frag_archives[1][:fragment_size], headers, 1),
            StubResponse(206, frag_archives[2][:fragment_size], headers, 2),
            StubResponse(206, frag_archives[3][:fragment_size], headers, 3),
            StubResponse(206, frag_archives[4][:fragment_size], headers, 4),
            # data nodes with old frag
            StubResponse(416, frag_index=5),
            StubResponse(416, frag_index=6),
            StubResponse(206, frag_archives[7][:fragment_size], headers, 7),
            StubResponse(206, frag_archives[8][:fragment_size], headers, 8),
            StubResponse(206, frag_archives[9][:fragment_size], headers, 9),
            # hopefully we ask for two more
            StubResponse(206, frag_archives[10][:fragment_size], headers, 10),
            StubResponse(206, frag_archives[11][:fragment_size], headers, 11),
        ]

        def get_response(req):
            return responses.pop(0) if responses else StubResponse(404)

        req = swob.Request.blank('/v1/a/c/o', headers={'Range': 'bytes=0-3'})
        with capture_http_requests(get_response) as log:
            resp = req.get_response(self.app)

        self.assertEqual(resp.status_int, 206)
        self.assertEqual(resp.body, 'test')
        self.assertEqual(len(log), self.policy.ec_ndata + 2)

        # verify that even when last responses to be collected are 416's
        # the shortfall of 2xx responses still triggers extra spawned requests
        responses = [
            StubResponse(206, frag_archives[0][:fragment_size], headers, 0),
            StubResponse(206, frag_archives[1][:fragment_size], headers, 1),
            StubResponse(206, frag_archives[2][:fragment_size], headers, 2),
            StubResponse(206, frag_archives[3][:fragment_size], headers, 3),
            StubResponse(206, frag_archives[4][:fragment_size], headers, 4),
            StubResponse(206, frag_archives[7][:fragment_size], headers, 7),
            StubResponse(206, frag_archives[8][:fragment_size], headers, 8),
            StubResponse(206, frag_archives[9][:fragment_size], headers, 9),
            StubResponse(206, frag_archives[10][:fragment_size], headers, 10),
            # data nodes with old frag
            StubResponse(416, frag_index=5),
            # hopefully we ask for one more
            StubResponse(416, frag_index=6),
            # and hopefully we ask for another
            StubResponse(206, frag_archives[11][:fragment_size], headers, 11),
        ]

        req = swob.Request.blank('/v1/a/c/o', headers={'Range': 'bytes=0-3'})
        with capture_http_requests(get_response) as log:
            resp = req.get_response(self.app)

        self.assertEqual(resp.status_int, 206)
        self.assertEqual(resp.body, 'test')
        self.assertEqual(len(log), self.policy.ec_ndata + 2)

    def test_GET_with_range_unsatisfiable_mixed_success(self):
        responses = [
            StubResponse(416, frag_index=0),
            StubResponse(416, frag_index=1),
            StubResponse(416, frag_index=2),
            StubResponse(416, frag_index=3),
            StubResponse(416, frag_index=4),
            StubResponse(416, frag_index=5),
            StubResponse(416, frag_index=6),
            # sneak in bogus extra responses
            StubResponse(404),
            StubResponse(206, frag_index=8),
            # and then just "enough" more 416's
            StubResponse(416, frag_index=9),
            StubResponse(416, frag_index=10),
            StubResponse(416, frag_index=11),
        ]

        def get_response(req):
            return responses.pop(0) if responses else StubResponse(404)

        req = swob.Request.blank('/v1/a/c/o', headers={
            'Range': 'bytes=%s-' % 100000000000000})
        with capture_http_requests(get_response) as log:
            resp = req.get_response(self.app)

        self.assertEqual(resp.status_int, 416)
        # ec_ndata responses that must agree, plus the bogus extras
        self.assertEqual(len(log), self.policy.ec_ndata + 2)

    def test_GET_with_missing_and_range_unsatisifiable(self):
        responses = [  # not quite ec_ndata frags on primaries
            StubResponse(416, frag_index=0),
            StubResponse(416, frag_index=1),
            StubResponse(416, frag_index=2),
            StubResponse(416, frag_index=3),
            StubResponse(416, frag_index=4),
            StubResponse(416, frag_index=5),
            StubResponse(416, frag_index=6),
            StubResponse(416, frag_index=7),
            StubResponse(416, frag_index=8),
        ]

        def get_response(req):
            return responses.pop(0) if responses else StubResponse(404)

        req = swob.Request.blank('/v1/a/c/o', headers={
            'Range': 'bytes=%s-' % 100000000000000})
        with capture_http_requests(get_response) as log:
            resp = req.get_response(self.app)

        # TODO: does 416 make sense without a quorum, or should this be a 404?
        # a non-range GET of same object would return 404
        self.assertEqual(resp.status_int, 416)
        self.assertEqual(len(log), 2 * self.replicas())

    def test_GET_with_success_and_507_will_503(self):
        responses = [  # only 9 good nodes
            StubResponse(200),
            StubResponse(200),
            StubResponse(200),
            StubResponse(200),
            StubResponse(200),
            StubResponse(200),
            StubResponse(200),
            StubResponse(200),
            StubResponse(200),
        ]

        def get_response(req):
            # bad disk on all other nodes
            return responses.pop(0) if responses else StubResponse(507)

        req = swob.Request.blank('/v1/a/c/o')
        with capture_http_requests(get_response) as log:
            resp = req.get_response(self.app)

        self.assertEqual(resp.status_int, 503)
        self.assertEqual(len(log), 2 * self.replicas())

    def test_GET_with_success_and_404_will_404(self):
        responses = [  # only 9 good nodes
            StubResponse(200),
            StubResponse(200),
            StubResponse(200),
            StubResponse(200),
            StubResponse(200),
            StubResponse(200),
            StubResponse(200),
            StubResponse(200),
            StubResponse(200),
        ]

        def get_response(req):
            # no frags on other nodes
            return responses.pop(0) if responses else StubResponse(404)

        req = swob.Request.blank('/v1/a/c/o')
        with capture_http_requests(get_response) as log:
            resp = req.get_response(self.app)

        self.assertEqual(resp.status_int, 404)
        self.assertEqual(len(log), 2 * self.replicas())

    def test_GET_mixed_ranged_responses_success(self):
        segment_size = self.policy.ec_segment_size
        frag_size = self.policy.fragment_size
        new_data = ('test' * segment_size)[:-492]
        new_etag = md5(new_data).hexdigest()
        new_archives = self._make_ec_archive_bodies(new_data)
        old_data = ('junk' * segment_size)[:-492]
        old_etag = md5(old_data).hexdigest()
        old_archives = self._make_ec_archive_bodies(old_data)
        frag_archive_size = len(new_archives[0])

        # here we deliberately omit X-Backend-Data-Timestamp to check that
        # proxy will tolerate responses from object server that have not been
        # upgraded to send that header
        old_headers = {
            'Content-Type': 'text/plain',
            'Content-Length': frag_size,
            'Content-Range': 'bytes 0-%s/%s' % (frag_size - 1,
                                                frag_archive_size),
            'X-Object-Sysmeta-Ec-Content-Length': len(old_data),
            'X-Object-Sysmeta-Ec-Etag': old_etag,
            'X-Backend-Timestamp': Timestamp(self._ts_iter.next()).internal
        }
        new_headers = {
            'Content-Type': 'text/plain',
            'Content-Length': frag_size,
            'Content-Range': 'bytes 0-%s/%s' % (frag_size - 1,
                                                frag_archive_size),
            'X-Object-Sysmeta-Ec-Content-Length': len(new_data),
            'X-Object-Sysmeta-Ec-Etag': new_etag,
            'X-Backend-Timestamp': Timestamp(self._ts_iter.next()).internal
        }
        # 7 primaries with stale frags, 3 handoffs failed to get new frags
        responses = [
            StubResponse(206, old_archives[0][:frag_size], old_headers, 0),
            StubResponse(206, new_archives[1][:frag_size], new_headers, 1),
            StubResponse(206, old_archives[2][:frag_size], old_headers, 2),
            StubResponse(206, new_archives[3][:frag_size], new_headers, 3),
            StubResponse(206, old_archives[4][:frag_size], old_headers, 4),
            StubResponse(206, new_archives[5][:frag_size], new_headers, 5),
            StubResponse(206, old_archives[6][:frag_size], old_headers, 6),
            StubResponse(206, new_archives[7][:frag_size], new_headers, 7),
            StubResponse(206, old_archives[8][:frag_size], old_headers, 8),
            StubResponse(206, new_archives[9][:frag_size], new_headers, 9),
            StubResponse(206, old_archives[10][:frag_size], old_headers, 10),
            StubResponse(206, new_archives[11][:frag_size], new_headers, 11),
            StubResponse(206, old_archives[12][:frag_size], old_headers, 12),
            StubResponse(206, new_archives[13][:frag_size], new_headers, 13),
            StubResponse(206, new_archives[0][:frag_size], new_headers, 0),
            StubResponse(404),
            StubResponse(404),
            StubResponse(206, new_archives[6][:frag_size], new_headers, 6),
            StubResponse(404),
            StubResponse(206, new_archives[10][:frag_size], new_headers, 10),
            StubResponse(206, new_archives[12][:frag_size], new_headers, 12),
        ]

        def get_response(req):
            return responses.pop(0) if responses else StubResponse(404)

        req = swob.Request.blank('/v1/a/c/o')
        with capture_http_requests(get_response) as log:
            resp = req.get_response(self.app)

        self.assertEqual(resp.status_int, 200)
        self.assertEqual(resp.body, new_data[:segment_size])
        self.assertEqual(len(log), self.policy.ec_ndata + 10)

    def test_GET_mismatched_fragment_archives(self):
        segment_size = self.policy.ec_segment_size
        test_data1 = ('test' * segment_size)[:-333]
        # N.B. the object data *length* here is different
        test_data2 = ('blah1' * segment_size)[:-333]

        etag1 = md5(test_data1).hexdigest()
        etag2 = md5(test_data2).hexdigest()

        ec_archive_bodies1 = self._make_ec_archive_bodies(test_data1)
        ec_archive_bodies2 = self._make_ec_archive_bodies(test_data2)

        headers1 = {'X-Object-Sysmeta-Ec-Etag': etag1,
                    'X-Object-Sysmeta-Ec-Content-Length': '333'}
        # here we're going to *lie* and say the etag here matches
        headers2 = {'X-Object-Sysmeta-Ec-Etag': etag1,
                    'X-Object-Sysmeta-Ec-Content-Length': '333'}

        responses1 = [(200, body, self._add_frag_index(fi, headers1))
                      for fi, body in enumerate(ec_archive_bodies1)]
        responses2 = [(200, body, self._add_frag_index(fi, headers2))
                      for fi, body in enumerate(ec_archive_bodies2)]

        req = swob.Request.blank('/v1/a/c/o')

        # sanity check responses1
        responses = responses1[:self.policy.ec_ndata]
        status_codes, body_iter, headers = zip(*responses)
        with set_http_connect(*status_codes, body_iter=body_iter,
                              headers=headers):
            resp = req.get_response(self.app)
        self.assertEqual(resp.status_int, 200)
        self.assertEqual(md5(resp.body).hexdigest(), etag1)

        # sanity check responses2
        responses = responses2[:self.policy.ec_ndata]
        status_codes, body_iter, headers = zip(*responses)
        with set_http_connect(*status_codes, body_iter=body_iter,
                              headers=headers):
            resp = req.get_response(self.app)
        self.assertEqual(resp.status_int, 200)
        self.assertEqual(md5(resp.body).hexdigest(), etag2)

        # now mix the responses a bit
        mix_index = random.randint(0, self.policy.ec_ndata - 1)
        mixed_responses = responses1[:self.policy.ec_ndata]
        mixed_responses[mix_index] = responses2[mix_index]

        status_codes, body_iter, headers = zip(*mixed_responses)
        with set_http_connect(*status_codes, body_iter=body_iter,
                              headers=headers):
            resp = req.get_response(self.app)
        self.assertEqual(resp.status_int, 200)
        try:
            resp.body
        except ECDriverError:
            resp._app_iter.close()
        else:
            self.fail('invalid ec fragment response body did not blow up!')
        error_lines = self.logger.get_lines_for_level('error')
        self.assertEqual(1, len(error_lines))
        msg = error_lines[0]
        self.assertIn('Error decoding fragments', msg)
        self.assertIn('/a/c/o', msg)
        log_msg_args, log_msg_kwargs = self.logger.log_dict['error'][0]
        self.assertEqual(log_msg_kwargs['exc_info'][0], ECDriverError)

    def test_GET_read_timeout(self):
        segment_size = self.policy.ec_segment_size
        test_data = ('test' * segment_size)[:-333]
        etag = md5(test_data).hexdigest()
        ec_archive_bodies = self._make_ec_archive_bodies(test_data)
        headers = {'X-Object-Sysmeta-Ec-Etag': etag}
        self.app.recoverable_node_timeout = 0.01
        responses = [(200, SlowBody(body, 0.1),
                      self._add_frag_index(i, headers))
                     for i, body in enumerate(ec_archive_bodies)]

        req = swob.Request.blank('/v1/a/c/o')

        status_codes, body_iter, headers = zip(*responses + [
            (404, '', {}) for i in range(
                self.policy.object_ring.max_more_nodes)])
        with set_http_connect(*status_codes, body_iter=body_iter,
                              headers=headers):
            resp = req.get_response(self.app)
            self.assertEqual(resp.status_int, 200)
            # do this inside the fake http context manager, it'll try to
            # resume but won't be able to give us all the right bytes
            self.assertNotEqual(md5(resp.body).hexdigest(), etag)
        error_lines = self.logger.get_lines_for_level('error')
        self.assertEqual(self.replicas(), len(error_lines))
        nparity = self.policy.ec_nparity
        for line in error_lines[:nparity]:
            self.assertIn('retrying', line)
        for line in error_lines[nparity:]:
            self.assertIn('ChunkReadTimeout (0.01s)', line)

    def test_GET_read_timeout_resume(self):
        segment_size = self.policy.ec_segment_size
        test_data = ('test' * segment_size)[:-333]
        etag = md5(test_data).hexdigest()
        ec_archive_bodies = self._make_ec_archive_bodies(test_data)
        headers = {'X-Object-Sysmeta-Ec-Etag': etag}
        self.app.recoverable_node_timeout = 0.05
        # first one is slow
        responses = [(200, SlowBody(ec_archive_bodies[0], 0.1),
                      self._add_frag_index(0, headers))]
        # ... the rest are fine
        responses += [(200, body, self._add_frag_index(i, headers))
                      for i, body in enumerate(ec_archive_bodies[1:], start=1)]

        req = swob.Request.blank('/v1/a/c/o')

        status_codes, body_iter, headers = zip(
            *responses[:self.policy.ec_ndata + 1])
        with set_http_connect(*status_codes, body_iter=body_iter,
                              headers=headers):
            resp = req.get_response(self.app)
            self.assertEqual(resp.status_int, 200)
            self.assertTrue(md5(resp.body).hexdigest(), etag)
        error_lines = self.logger.get_lines_for_level('error')
        self.assertEqual(1, len(error_lines))
        self.assertIn('retrying', error_lines[0])

    def test_fix_response_HEAD(self):
        headers = {'X-Object-Sysmeta-Ec-Content-Length': '10',
                   'X-Object-Sysmeta-Ec-Etag': 'foo'}

        # sucsessful HEAD
        responses = [(200, '', headers)]
        status_codes, body_iter, headers = zip(*responses)
        req = swift.common.swob.Request.blank('/v1/a/c/o', method='HEAD')
        with set_http_connect(*status_codes, body_iter=body_iter,
                              headers=headers):
            resp = req.get_response(self.app)
        self.assertEqual(resp.status_int, 200)
        self.assertEqual(resp.body, '')
        # 200OK shows original object content length
        self.assertEqual(resp.headers['Content-Length'], '10')
        self.assertEqual(resp.headers['Etag'], 'foo')

        # not found HEAD
        responses = [(404, '', {})] * self.replicas() * 2
        status_codes, body_iter, headers = zip(*responses)
        req = swift.common.swob.Request.blank('/v1/a/c/o', method='HEAD')
        with set_http_connect(*status_codes, body_iter=body_iter,
                              headers=headers):
            resp = req.get_response(self.app)
        self.assertEqual(resp.status_int, 404)
        # 404 shows actual response body size (i.e. 0 for HEAD)
        self.assertEqual(resp.headers['Content-Length'], '0')

    def test_PUT_with_slow_commits(self):
        # It's important that this timeout be much less than the delay in
        # the slow commit responses so that the slow commits are not waited
        # for.
        self.app.post_quorum_timeout = 0.01
        req = swift.common.swob.Request.blank('/v1/a/c/o', method='PUT',
                                              body='')
        # plenty of slow commits
        response_sleep = 5.0
        codes = [FakeStatus(201, response_sleep=response_sleep)
                 for i in range(self.replicas())]
        # swap out some with regular fast responses
        number_of_fast_responses_needed_to_be_quick_enough = \
            self.policy.quorum
        fast_indexes = random.sample(
            range(self.replicas()),
            number_of_fast_responses_needed_to_be_quick_enough)
        for i in fast_indexes:
            codes[i] = 201
        expect_headers = {
            'X-Obj-Metadata-Footer': 'yes',
            'X-Obj-Multiphase-Commit': 'yes'
        }
        with set_http_connect(*codes, expect_headers=expect_headers):
            start = time.time()
            resp = req.get_response(self.app)
            response_time = time.time() - start
        self.assertEqual(resp.status_int, 201)
        self.assertLess(response_time, response_sleep)

    def test_PUT_with_just_enough_durable_responses(self):
        req = swift.common.swob.Request.blank('/v1/a/c/o', method='PUT',
                                              body='')

        codes = [201] * (self.policy.ec_ndata + 1)
        codes += [503] * (self.policy.ec_nparity - 1)
        self.assertEqual(len(codes), self.replicas())
        random.shuffle(codes)
        expect_headers = {
            'X-Obj-Metadata-Footer': 'yes',
            'X-Obj-Multiphase-Commit': 'yes'
        }
        with set_http_connect(*codes, expect_headers=expect_headers):
            resp = req.get_response(self.app)
        self.assertEqual(resp.status_int, 201)

    def test_PUT_with_less_durable_responses(self):
        req = swift.common.swob.Request.blank('/v1/a/c/o', method='PUT',
                                              body='')

        codes = [201] * (self.policy.ec_ndata)
        codes += [503] * (self.policy.ec_nparity)
        self.assertEqual(len(codes), self.replicas())
        random.shuffle(codes)
        expect_headers = {
            'X-Obj-Metadata-Footer': 'yes',
            'X-Obj-Multiphase-Commit': 'yes'
        }
        with set_http_connect(*codes, expect_headers=expect_headers):
            resp = req.get_response(self.app)
        self.assertEqual(resp.status_int, 503)

    def test_GET_with_invalid_ranges(self):
        # real body size is segment_size - 10 (just 1 segment)
        segment_size = self.policy.ec_segment_size
        real_body = ('a' * segment_size)[:-10]

        # range is out of real body but in segment size
        self._test_invalid_ranges('GET', real_body,
                                  segment_size, '%s-' % (segment_size - 10))
        # range is out of both real body and segment size
        self._test_invalid_ranges('GET', real_body,
                                  segment_size, '%s-' % (segment_size + 10))

    def _test_invalid_ranges(self, method, real_body, segment_size, req_range):
        # make a request with range starts from more than real size.
        body_etag = md5(real_body).hexdigest()
        req = swift.common.swob.Request.blank(
            '/v1/a/c/o', method=method,
            headers={'Destination': 'c1/o',
                     'Range': 'bytes=%s' % (req_range)})

        fragments = self.policy.pyeclib_driver.encode(real_body)
        fragment_payloads = [fragments]

        node_fragments = zip(*fragment_payloads)
        self.assertEqual(len(node_fragments), self.replicas())  # sanity
        headers = {'X-Object-Sysmeta-Ec-Content-Length': str(len(real_body)),
                   'X-Object-Sysmeta-Ec-Etag': body_etag}
        start = int(req_range.split('-')[0])
        self.assertGreaterEqual(start, 0)  # sanity
        title, exp = swob.RESPONSE_REASONS[416]
        range_not_satisfiable_body = \
            '<html><h1>%s</h1><p>%s</p></html>' % (title, exp)
        if start >= segment_size:
            responses = [(416, range_not_satisfiable_body,
                          self._add_frag_index(i, headers))
                         for i in range(POLICIES.default.ec_ndata)]
        else:
            responses = [(200, ''.join(node_fragments[i]),
                          self._add_frag_index(i, headers))
                         for i in range(POLICIES.default.ec_ndata)]
        status_codes, body_iter, headers = zip(*responses)
        expect_headers = {
            'X-Obj-Metadata-Footer': 'yes',
            'X-Obj-Multiphase-Commit': 'yes'
        }
        with set_http_connect(*status_codes, body_iter=body_iter,
                              headers=headers, expect_headers=expect_headers):
            resp = req.get_response(self.app)
        self.assertEqual(resp.status_int, 416)
        self.assertEqual(resp.content_length, len(range_not_satisfiable_body))
        self.assertEqual(resp.body, range_not_satisfiable_body)
        self.assertEqual(resp.etag, body_etag)
        self.assertEqual(resp.headers['Accept-Ranges'], 'bytes')


if __name__ == '__main__':
    unittest.main()
