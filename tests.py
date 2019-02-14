# -*- coding: utf-8 -*-
"""
    proxy.py
    ~~~~~~~~

    HTTP Proxy Server in Python.

    :copyright: (c) 2013-2018 by Abhinav Singh.
    :license: BSD, see LICENSE for more details.
"""
import base64
import socket
import logging
import unittest
import urllib.request
from threading import Thread
from contextlib import closing
from proxy import HTTP, Proxy, ChunkParser, HttpParser, Client
from proxy import ProxyAuthenticationFailed, ProxyConnectionFailed
from proxy import version, PY3, CRLF, PROXY_TUNNEL_ESTABLISHED_RESPONSE_PKT, \
    DEFAULT_SERVER_RECVBUF_SIZE, DEFAULT_CLIENT_RECVBUF_SIZE, DEFAULT_LOGGING_FORMAT

logging.basicConfig(level=logging.DEBUG, format=DEFAULT_LOGGING_FORMAT)

if PY3:
    from http.server import HTTPServer, BaseHTTPRequestHandler
else:
    from BaseHTTPServer import HTTPServer, BaseHTTPRequestHandler


class TestChunkParser(unittest.TestCase):

    def setUp(self):
        self.parser = ChunkParser()

    def test_chunk_parse_basic(self):
        self.parser.parse(b''.join([
            b'4\r\n',
            b'Wiki\r\n',
            b'5\r\n',
            b'pedia\r\n',
            b'E\r\n',
            b' in\r\n\r\nchunks.\r\n',
            b'0\r\n',
            b'\r\n'
        ]))
        self.assertEqual(self.parser.chunk, b'')
        self.assertEqual(self.parser.size, None)
        self.assertEqual(self.parser.body, b'Wikipedia in\r\n\r\nchunks.')
        self.assertEqual(self.parser.state, ChunkParser.states.COMPLETE)

    def test_chunk_parse_issue_27(self):
        """Case when data ends with the chunk size but without CRLF."""
        self.parser.parse(b'3')
        self.assertEqual(self.parser.chunk, b'3')
        self.assertEqual(self.parser.size, None)
        self.assertEqual(self.parser.body, b'')
        self.assertEqual(self.parser.state, ChunkParser.states.WAITING_FOR_SIZE)
        self.parser.parse(b'\r\n')
        self.assertEqual(self.parser.chunk, b'')
        self.assertEqual(self.parser.size, 3)
        self.assertEqual(self.parser.body, b'')
        self.assertEqual(self.parser.state, ChunkParser.states.WAITING_FOR_DATA)
        self.parser.parse(b'abc')
        self.assertEqual(self.parser.chunk, b'')
        self.assertEqual(self.parser.size, None)
        self.assertEqual(self.parser.body, b'abc')
        self.assertEqual(self.parser.state, ChunkParser.states.WAITING_FOR_SIZE)
        self.parser.parse(b'\r\n')
        self.assertEqual(self.parser.chunk, b'')
        self.assertEqual(self.parser.size, None)
        self.assertEqual(self.parser.body, b'abc')
        self.assertEqual(self.parser.state, ChunkParser.states.WAITING_FOR_SIZE)
        self.parser.parse(b'4\r\n')
        self.assertEqual(self.parser.chunk, b'')
        self.assertEqual(self.parser.size, 4)
        self.assertEqual(self.parser.body, b'abc')
        self.assertEqual(self.parser.state, ChunkParser.states.WAITING_FOR_DATA)
        self.parser.parse(b'defg\r\n0')
        self.assertEqual(self.parser.chunk, b'0')
        self.assertEqual(self.parser.size, None)
        self.assertEqual(self.parser.body, b'abcdefg')
        self.assertEqual(self.parser.state, ChunkParser.states.WAITING_FOR_SIZE)
        self.parser.parse(b'\r\n\r\n')
        self.assertEqual(self.parser.chunk, b'')
        self.assertEqual(self.parser.size, None)
        self.assertEqual(self.parser.body, b'abcdefg')
        self.assertEqual(self.parser.state, ChunkParser.states.COMPLETE)


class TestHttpParser(unittest.TestCase):

    def setUp(self):
        self.parser = HttpParser(HttpParser.types.REQUEST_PARSER)

    def test_build_header(self):
        self.assertEqual(HttpParser.build_header(b'key', b'value'), b'key: value')

    def test_split(self):
        self.assertEqual(HttpParser.split(b'CONNECT python.org:443 HTTP/1.0\r\n\r\n'),
                         (b'CONNECT python.org:443 HTTP/1.0', b'\r\n'))

    def test_split_false_line(self):
        self.assertEqual(HttpParser.split(b'CONNECT python.org:443 HTTP/1.0'),
                         (False, b'CONNECT python.org:443 HTTP/1.0'))

    def test_get_full_parse(self):
        raw = CRLF.join([
            b'GET %s HTTP/1.1',
            b'Host: %s',
            CRLF
        ])
        self.parser.parse(raw % (b'https://example.com/path/dir/?a=b&c=d#p=q', b'example.com'))
        self.assertEqual(self.parser.build_url(), b'/path/dir/?a=b&c=d#p=q')
        self.assertEqual(self.parser.method, b'GET')
        self.assertEqual(self.parser.url.hostname, b'example.com')
        self.assertEqual(self.parser.url.port, None)
        self.assertEqual(self.parser.version, b'HTTP/1.1')
        self.assertEqual(self.parser.state, HttpParser.states.COMPLETE)
        self.assertDictContainsSubset({b'host': (b'Host', b'example.com')}, self.parser.headers)
        self.assertEqual(raw % (b'/path/dir/?a=b&c=d#p=q', b'example.com'),
                         self.parser.build(del_headers=[b'host'], add_headers=[(b'Host', b'example.com')]))

    def test_build_url_none(self):
        self.assertEqual(self.parser.build_url(), b'/None')

    def test_line_rcvd_to_rcving_headers_state_change(self):
        self.parser.parse(b'GET http://localhost HTTP/1.1')
        self.assertEqual(self.parser.state, HttpParser.states.INITIALIZED)
        self.parser.parse(CRLF)
        self.assertEqual(self.parser.state, HttpParser.states.LINE_RCVD)
        self.parser.parse(CRLF)
        self.assertEqual(self.parser.state, HttpParser.states.RCVING_HEADERS)

    def test_get_partial_parse1(self):
        self.parser.parse(CRLF.join([
            b'GET http://localhost:8080 HTTP/1.1'
        ]))
        self.assertEqual(self.parser.method, None)
        self.assertEqual(self.parser.url, None)
        self.assertEqual(self.parser.version, None)
        self.assertEqual(self.parser.state, HttpParser.states.INITIALIZED)

        self.parser.parse(CRLF)
        self.assertEqual(self.parser.method, b'GET')
        self.assertEqual(self.parser.url.hostname, b'localhost')
        self.assertEqual(self.parser.url.port, 8080)
        self.assertEqual(self.parser.version, b'HTTP/1.1')
        self.assertEqual(self.parser.state, HttpParser.states.LINE_RCVD)

        self.parser.parse(b'Host: localhost:8080')
        self.assertDictEqual(self.parser.headers, dict())
        self.assertEqual(self.parser.buffer, b'Host: localhost:8080')
        self.assertEqual(self.parser.state, HttpParser.states.LINE_RCVD)

        self.parser.parse(CRLF * 2)
        self.assertDictContainsSubset({b'host': (b'Host', b'localhost:8080')}, self.parser.headers)
        self.assertEqual(self.parser.state, HttpParser.states.COMPLETE)

    def test_get_partial_parse2(self):
        self.parser.parse(CRLF.join([
            b'GET http://localhost:8080 HTTP/1.1',
            b'Host: '
        ]))
        self.assertEqual(self.parser.method, b'GET')
        self.assertEqual(self.parser.url.hostname, b'localhost')
        self.assertEqual(self.parser.url.port, 8080)
        self.assertEqual(self.parser.version, b'HTTP/1.1')
        self.assertEqual(self.parser.buffer, b'Host: ')
        self.assertEqual(self.parser.state, HttpParser.states.LINE_RCVD)

        self.parser.parse(b'localhost:8080' + CRLF)
        self.assertDictContainsSubset({b'host': (b'Host', b'localhost:8080')}, self.parser.headers)
        self.assertEqual(self.parser.buffer, b'')
        self.assertEqual(self.parser.state, HttpParser.states.RCVING_HEADERS)

        self.parser.parse(b'Content-Type: text/plain' + CRLF)
        self.assertEqual(self.parser.buffer, b'')
        self.assertDictContainsSubset({b'content-type': (b'Content-Type', b'text/plain')}, self.parser.headers)
        self.assertEqual(self.parser.state, HttpParser.states.RCVING_HEADERS)

        self.parser.parse(CRLF)
        self.assertEqual(self.parser.state, HttpParser.states.COMPLETE)

    def test_post_full_parse(self):
        raw = CRLF.join([
            b'POST %s HTTP/1.1',
            b'Host: localhost',
            b'Content-Length: 7',
            b'Content-Type: application/x-www-form-urlencoded' + CRLF,
            b'a=b&c=d'
        ])
        self.parser.parse(raw % b'http://localhost')
        self.assertEqual(self.parser.method, b'POST')
        self.assertEqual(self.parser.url.hostname, b'localhost')
        self.assertEqual(self.parser.url.port, None)
        self.assertEqual(self.parser.version, b'HTTP/1.1')
        self.assertDictContainsSubset({b'content-type': (b'Content-Type', b'application/x-www-form-urlencoded')},
                                      self.parser.headers)
        self.assertDictContainsSubset({b'content-length': (b'Content-Length', b'7')}, self.parser.headers)
        self.assertEqual(self.parser.body, b'a=b&c=d')
        self.assertEqual(self.parser.buffer, b'')
        self.assertEqual(self.parser.state, HttpParser.states.COMPLETE)
        self.assertEqual(len(self.parser.build()), len(raw % b'/'))

    def test_post_partial_parse(self):
        self.parser.parse(CRLF.join([
            b'POST http://localhost HTTP/1.1',
            b'Host: localhost',
            b'Content-Length: 7',
            b'Content-Type: application/x-www-form-urlencoded'
        ]))
        self.assertEqual(self.parser.method, b'POST')
        self.assertEqual(self.parser.url.hostname, b'localhost')
        self.assertEqual(self.parser.url.port, None)
        self.assertEqual(self.parser.version, b'HTTP/1.1')
        self.assertEqual(self.parser.state, HttpParser.states.RCVING_HEADERS)

        self.parser.parse(CRLF)
        self.assertEqual(self.parser.state, HttpParser.states.RCVING_HEADERS)

        self.parser.parse(CRLF)
        self.assertEqual(self.parser.state, HttpParser.states.HEADERS_COMPLETE)

        self.parser.parse(b'a=b')
        self.assertEqual(self.parser.state, HttpParser.states.RCVING_BODY)
        self.assertEqual(self.parser.body, b'a=b')
        self.assertEqual(self.parser.buffer, b'')

        self.parser.parse(b'&c=d')
        self.assertEqual(self.parser.state, HttpParser.states.COMPLETE)
        self.assertEqual(self.parser.body, b'a=b&c=d')
        self.assertEqual(self.parser.buffer, b'')

    def test_connect_request_without_host_header_request_parse(self):
        """Case where clients can send CONNECT request without a Host header field.

        Example:
            1. pip3 --proxy http://localhost:8899 install <package name>
               Uses HTTP/1.0, Host header missing with CONNECT requests
            2. Android Emulator
               Uses HTTP/1.1, Host header missing with CONNECT requests

        See https://github.com/abhinavsingh/proxy.py/issues/5 for details.
        """
        self.parser.parse(b'CONNECT pypi.org:443 HTTP/1.0\r\n\r\n')
        self.assertEqual(self.parser.method, b'CONNECT')
        self.assertEqual(self.parser.version, b'HTTP/1.0')
        self.assertEqual(self.parser.state, HttpParser.states.COMPLETE)

    def test_request_parse_without_content_length(self):
        """Case when incoming request doesn't contain a content-length header.

        From http://w3-org.9356.n7.nabble.com/POST-with-empty-body-td103965.html
        'A POST with no content-length and no body is equivalent to a POST with Content-Length: 0
        and nothing following, as could perfectly happen when you upload an empty file for instance.'

        See https://github.com/abhinavsingh/proxy.py/issues/20 for details.
        """
        self.parser.parse(CRLF.join([
            b'POST http://localhost HTTP/1.1',
            b'Host: localhost',
            b'Content-Type: application/x-www-form-urlencoded',
            CRLF
        ]))
        self.assertEqual(self.parser.method, b'POST')
        self.assertEqual(self.parser.state, HttpParser.states.COMPLETE)

    def test_cannot_parse_malformed_request_line(self):
        self.parser.parse(CRLF.join([
            b'GET       http://localhost        HTTP/1.1',
            b'Host: localhost',
            CRLF
        ]))
        self.assertEqual(self.parser.method, b'GET')
        self.assertNotEqual(self.parser.url.scheme, b'http')
        self.assertNotEqual(self.parser.url.netloc, b'localhost')
        self.assertEqual(self.parser.url.path, b'')
        self.assertEqual(self.parser.url.query, b'')
        self.assertEqual(self.parser.url.fragment, b'')
        self.assertNotEqual(self.parser.version, b'HTTP/1.1')
        self.assertEqual(self.parser.state, HttpParser.states.COMPLETE)

    def test_response_parse_without_content_length(self):
        """Case when server response doesn't contain a content-length header for non-chunk response types.

        HttpParser by itself has no way to know if more data should be expected.
        In example below, parser reaches state HttpParser.states.HEADERS_COMPLETE
        and it is responsibility of callee to change state to HttpParser.states.COMPLETE
        when server stream closes.

        See https://github.com/abhinavsingh/proxy.py/issues/20 for details.
        """
        self.parser.type = HttpParser.types.RESPONSE_PARSER
        self.parser.parse(b'HTTP/1.0 200 OK' + CRLF)
        self.assertEqual(self.parser.code, b'200')
        self.assertEqual(self.parser.version, b'HTTP/1.0')
        self.assertEqual(self.parser.state, HttpParser.states.LINE_RCVD)
        self.parser.parse(CRLF.join([
            b'Server: BaseHTTP/0.3 Python/2.7.10',
            b'Date: Thu, 13 Dec 2018 16:24:09 GMT',
            CRLF
        ]))
        self.assertEqual(self.parser.state, HttpParser.states.HEADERS_COMPLETE)

    def test_response_parse(self):
        self.parser.type = HttpParser.types.RESPONSE_PARSER
        self.parser.parse(b''.join([
            b'HTTP/1.1 301 Moved Permanently\r\n',
            b'Location: http://www.google.com/\r\n',
            b'Content-Type: text/html; charset=UTF-8\r\n',
            b'Date: Wed, 22 May 2013 14:07:29 GMT\r\n',
            b'Expires: Fri, 21 Jun 2013 14:07:29 GMT\r\n',
            b'Cache-Control: public, max-age=2592000\r\n',
            b'Server: gws\r\n',
            b'Content-Length: 219\r\n',
            b'X-XSS-Protection: 1; mode=block\r\n',
            b'X-Frame-Options: SAMEORIGIN\r\n\r\n',
            b'<HTML><HEAD><meta http-equiv="content-type" content="text/html;charset=utf-8">\n' +
            b'<TITLE>301 Moved</TITLE></HEAD>',
            b'<BODY>\n<H1>301 Moved</H1>\nThe document has moved\n' +
            b'<A HREF="http://www.google.com/">here</A>.\r\n</BODY></HTML>\r\n'
        ]))
        self.assertEqual(self.parser.code, b'301')
        self.assertEqual(self.parser.reason, b'Moved Permanently')
        self.assertEqual(self.parser.version, b'HTTP/1.1')
        self.assertEqual(self.parser.body,
                         b'<HTML><HEAD><meta http-equiv="content-type" content="text/html;charset=utf-8">\n' +
                         b'<TITLE>301 Moved</TITLE></HEAD><BODY>\n<H1>301 Moved</H1>\nThe document has moved\n' +
                         b'<A HREF="http://www.google.com/">here</A>.\r\n</BODY></HTML>\r\n')
        self.assertDictContainsSubset({b'content-length': (b'Content-Length', b'219')}, self.parser.headers)
        self.assertEqual(self.parser.state, HttpParser.states.COMPLETE)

    def test_response_partial_parse(self):
        self.parser.type = HttpParser.types.RESPONSE_PARSER
        self.parser.parse(b''.join([
            b'HTTP/1.1 301 Moved Permanently\r\n',
            b'Location: http://www.google.com/\r\n',
            b'Content-Type: text/html; charset=UTF-8\r\n',
            b'Date: Wed, 22 May 2013 14:07:29 GMT\r\n',
            b'Expires: Fri, 21 Jun 2013 14:07:29 GMT\r\n',
            b'Cache-Control: public, max-age=2592000\r\n',
            b'Server: gws\r\n',
            b'Content-Length: 219\r\n',
            b'X-XSS-Protection: 1; mode=block\r\n',
            b'X-Frame-Options: SAMEORIGIN\r\n'
        ]))
        self.assertDictContainsSubset({b'x-frame-options': (b'X-Frame-Options', b'SAMEORIGIN')}, self.parser.headers)
        self.assertEqual(self.parser.state, HttpParser.states.RCVING_HEADERS)
        self.parser.parse(b'\r\n')
        self.assertEqual(self.parser.state, HttpParser.states.HEADERS_COMPLETE)
        self.parser.parse(
            b'<HTML><HEAD><meta http-equiv="content-type" content="text/html;charset=utf-8">\n' +
            b'<TITLE>301 Moved</TITLE></HEAD>')
        self.assertEqual(self.parser.state, HttpParser.states.RCVING_BODY)
        self.parser.parse(
            b'<BODY>\n<H1>301 Moved</H1>\nThe document has moved\n' +
            b'<A HREF="http://www.google.com/">here</A>.\r\n</BODY></HTML>\r\n')
        self.assertEqual(self.parser.state, HttpParser.states.COMPLETE)

    def test_chunked_response_parse(self):
        self.parser.type = HttpParser.types.RESPONSE_PARSER
        self.parser.parse(b''.join([
            b'HTTP/1.1 200 OK\r\n',
            b'Content-Type: application/json\r\n',
            b'Date: Wed, 22 May 2013 15:08:15 GMT\r\n',
            b'Server: gunicorn/0.16.1\r\n',
            b'transfer-encoding: chunked\r\n',
            b'Connection: keep-alive\r\n\r\n',
            b'4\r\n',
            b'Wiki\r\n',
            b'5\r\n',
            b'pedia\r\n',
            b'E\r\n',
            b' in\r\n\r\nchunks.\r\n',
            b'0\r\n',
            b'\r\n'
        ]))
        self.assertEqual(self.parser.body, b'Wikipedia in\r\n\r\nchunks.')
        self.assertEqual(self.parser.state, HttpParser.states.COMPLETE)


class MockConnection(object):

    def __init__(self, b=b''):
        self.buffer = b

    def recv(self, b=8192):
        data = self.buffer[:b]
        self.buffer = self.buffer[b:]
        return data

    def send(self, data):
        return len(data)

    def queue(self, data):
        self.buffer += data

    def close(self):
        pass


def get_available_port():
    """Finds and retuns an available port on the system."""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(('', 0))
        _, port = sock.getsockname()
        return port


class HTTPRequestHandler(BaseHTTPRequestHandler):
    """Http request handler which returns a static hardcoded response for all requested GET URLs."""

    def do_GET(self):
        logging.info('request received')
        self.send_response(200)
        # TODO(abhinavsingh): Proxy should work just fine even without content-length header
        self.send_header('content-length', 2)
        self.end_headers()
        self.wfile.write(b'OK')


def start_http_server(http_handler=HTTPRequestHandler):
    """Starts a HTTP server on a random port in a separate thread."""
    http_server_port = get_available_port()
    logging.info('Starting http server for test on port %d' % http_server_port)
    http_server = HTTPServer(('127.0.0.1', http_server_port), http_handler)
    http_server_thread = Thread(target=http_server.serve_forever)
    http_server_thread.setDaemon(True)
    http_server_thread.start()
    return http_server_port, http_server, http_server_thread


class TestProxy(unittest.TestCase):

    http_server = None
    http_server_port = None
    http_server_thread = None

    @classmethod
    def setUpClass(cls):
        cls.http_server_port, cls.http_server, cls.http_server_thread = start_http_server()

    @classmethod
    def tearDownClass(cls):
        cls.http_server.shutdown()
        cls.http_server.server_close()
        cls.http_server_thread.join()

    def setUp(self):
        self._conn = MockConnection()
        self._addr = ('127.0.0.1', 54382)
        self.proxy = Proxy(self._conn, self._addr)

    def test_http_get(self):
        # Send request line
        self.proxy.client.conn.queue((b'GET http://localhost:%d HTTP/1.1' % self.http_server_port) + CRLF)
        self.proxy.process_request(self.proxy.client.recv(DEFAULT_CLIENT_RECVBUF_SIZE))
        self.assertNotEqual(self.proxy.request.state, HttpParser.states.COMPLETE)
        # Send headers and blank line, thus completing HTTP request
        self.proxy.client.conn.queue(CRLF.join([
            b'User-Agent: proxy.py/%s' % version,
            b'Host: localhost:%d' % self.http_server_port,
            b'Accept: */*',
            b'Proxy-Connection: Keep-Alive',
            CRLF
        ]))
        self.proxy.process_request(self.proxy.client.recv(DEFAULT_CLIENT_RECVBUF_SIZE))
        self.assertEqual(self.proxy.request.state, HttpParser.states.COMPLETE)
        self.assertEqual(self.proxy.server.addr, (b'localhost', self.http_server_port))
        # Flush data queued for server
        self.proxy.server.flush()
        self.assertEqual(self.proxy.server.buffer_size(), 0)
        # Receive full response from server
        data = self.proxy.server.recv(DEFAULT_SERVER_RECVBUF_SIZE)
        while data:
            self.proxy.process_response(data)
            logging.info(self.proxy.response.state)
            if self.proxy.response.state == HttpParser.states.COMPLETE:
                break
            data = self.proxy.server.recv(DEFAULT_SERVER_RECVBUF_SIZE)
        # Verify 200 success response code
        self.assertEqual(self.proxy.response.state, HttpParser.states.COMPLETE)
        self.assertEqual(int(self.proxy.response.code), 200)

    def test_http_tunnel(self):
        self.proxy.client.conn.queue(CRLF.join([
            b'CONNECT localhost:%d HTTP/1.1' % self.http_server_port,
            b'Host: localhost:%d' % self.http_server_port,
            b'User-Agent: proxy.py/%s' % version,
            b'Proxy-Connection: Keep-Alive',
            CRLF
        ]))
        self.proxy.process_request(self.proxy.client.recv(DEFAULT_CLIENT_RECVBUF_SIZE))
        self.assertFalse(self.proxy.server is None)
        self.assertEqual(self.proxy.client.buffer, PROXY_TUNNEL_ESTABLISHED_RESPONSE_PKT)

        parser = HttpParser(HttpParser.types.RESPONSE_PARSER)
        parser.parse(self.proxy.client.buffer)
        self.assertEqual(parser.state, HttpParser.states.HEADERS_COMPLETE)
        self.assertEqual(int(parser.code), 200)

        self.proxy.client.flush()
        self.assertEqual(self.proxy.client.buffer_size(), 0)

        self.proxy.client.conn.queue(CRLF.join([
            b'GET / HTTP/1.1',
            b'Host: localhost:%d' % self.http_server_port,
            b'User-Agent: proxy.py/%s' % version,
            CRLF
        ]))
        self.proxy.process_request(self.proxy.client.recv(DEFAULT_CLIENT_RECVBUF_SIZE))
        self.proxy.server.flush()
        self.assertEqual(self.proxy.server.buffer_size(), 0)

        parser = HttpParser(HttpParser.types.RESPONSE_PARSER)
        data = self.proxy.server.recv(DEFAULT_SERVER_RECVBUF_SIZE)
        while data:
            parser.parse(data)
            if parser.state == HttpParser.states.COMPLETE:
                break
            data = self.proxy.server.recv(DEFAULT_SERVER_RECVBUF_SIZE)

        self.assertEqual(parser.state, HttpParser.states.COMPLETE)
        self.assertEqual(int(parser.code), 200)

    def test_proxy_connection_failed(self):
        with self.assertRaises(ProxyConnectionFailed):
            self.proxy.process_request(CRLF.join([
                b'GET http://unknown.domain HTTP/1.1',
                b'Host: unknown.domain',
                CRLF
            ]))

    def test_proxy_authentication_failed(self):
        self.proxy = Proxy(self._conn, self._addr, auth_code=b'Basic %s' % base64.b64encode(b'user:pass'))

        with self.assertRaises(ProxyAuthenticationFailed):
            self.proxy.process_request(CRLF.join([
                b'GET http://abhinavsingh.com HTTP/1.1',
                b'Host: abhinavsingh.com',
                CRLF
            ]))

    def test_authenticated_proxy_http_get(self):
        self.proxy = Proxy(self._conn, self._addr, auth_code=b'Basic %s' % base64.b64encode(b'user:pass'))

        self.proxy.client.conn.queue((b'GET http://localhost:%d HTTP/1.1' % self.http_server_port) + CRLF)
        self.proxy.process_request(self.proxy.client.recv(DEFAULT_CLIENT_RECVBUF_SIZE))
        self.assertNotEqual(self.proxy.request.state, HttpParser.states.COMPLETE)

        self.proxy.client.conn.queue(CRLF.join([
            b'User-Agent: proxy.py/%s' % version,
            b'Host: localhost:%d' % self.http_server_port,
            b'Accept: */*',
            b'Proxy-Connection: Keep-Alive',
            b'Proxy-Authorization: Basic dXNlcjpwYXNz',
            CRLF
        ]))

        self.proxy.process_request(self.proxy.client.recv(DEFAULT_CLIENT_RECVBUF_SIZE))
        self.assertEqual(self.proxy.request.state, HttpParser.states.COMPLETE)
        self.assertEqual(self.proxy.server.addr, (b'localhost', self.http_server_port))

        self.proxy.server.flush()
        self.assertEqual(self.proxy.server.buffer_size(), 0)

        data = self.proxy.server.recv(DEFAULT_SERVER_RECVBUF_SIZE)
        while data:
            self.proxy.process_response(data)
            if self.proxy.response.state == HttpParser.states.COMPLETE:
                break
            data = self.proxy.server.recv(DEFAULT_SERVER_RECVBUF_SIZE)

        self.assertEqual(self.proxy.response.state, HttpParser.states.COMPLETE)
        self.assertEqual(int(self.proxy.response.code), 200)

    def test_authenticated_proxy_http_tunnel(self):
        self.proxy = Proxy(self._conn, self._addr, auth_code=b'Basic %s' % base64.b64encode(b'user:pass'))

        self.proxy.client.conn.queue(CRLF.join([
            b'CONNECT localhost:%d HTTP/1.1' % self.http_server_port,
            b'Host: localhost:%d' % self.http_server_port,
            b'User-Agent: proxy.py/%s' % version,
            b'Proxy-Connection: Keep-Alive',
            b'Proxy-Authorization: Basic dXNlcjpwYXNz',
            CRLF
        ]))
        self.proxy.process_request(self.proxy.client.recv(DEFAULT_CLIENT_RECVBUF_SIZE))
        self.assertFalse(self.proxy.server is None)
        self.assertEqual(self.proxy.client.buffer, PROXY_TUNNEL_ESTABLISHED_RESPONSE_PKT)

        parser = HttpParser(HttpParser.types.RESPONSE_PARSER)
        parser.parse(self.proxy.client.buffer)
        self.assertEqual(parser.state, HttpParser.states.HEADERS_COMPLETE)
        self.assertEqual(int(parser.code), 200)

        self.proxy.client.flush()
        self.assertEqual(self.proxy.client.buffer_size(), 0)

        self.proxy.client.conn.queue(CRLF.join([
            b'GET / HTTP/1.1',
            b'Host: localhost:%d' % self.http_server_port,
            b'User-Agent: proxy.py/%s' % version,
            CRLF
        ]))
        self.proxy.process_request(self.proxy.client.recv(DEFAULT_CLIENT_RECVBUF_SIZE))
        self.proxy.server.flush()
        self.assertEqual(self.proxy.server.buffer_size(), 0)

        parser = HttpParser(HttpParser.types.RESPONSE_PARSER)
        data = self.proxy.server.recv(DEFAULT_SERVER_RECVBUF_SIZE)
        while data:
            parser.parse(data)
            if parser.state == HttpParser.states.COMPLETE:
                break
            data = self.proxy.server.recv(DEFAULT_SERVER_RECVBUF_SIZE)

        self.assertEqual(parser.state, HttpParser.states.COMPLETE)
        self.assertEqual(int(parser.code), 200)


def start_proxy_server():
    """Starts proxy.py server on a random port in a separate thread."""
    proxy_server_port = get_available_port()
    proxy_server = HTTP(port=proxy_server_port)
    proxy_server_thread = Thread(target=proxy_server.run)
    proxy_server_thread.setDaemon(True)
    proxy_server_thread.start()
    return proxy_server_port, proxy_server, proxy_server_thread


class TestWorkers(unittest.TestCase):

    http_server = None
    http_server_port = None
    http_server_thread = None

    proxy_server = None
    proxy_server_port = None
    proxy_server_thread = None

    @classmethod
    def setUpClass(cls):
        cls.http_server_port, cls.http_server, cls.http_server_thread = start_http_server()
        cls.proxy_server_port, cls.proxy_server, cls.proxy_server_thread = start_proxy_server()

    @classmethod
    def tearDownClass(cls):
        cls.http_server.shutdown()
        cls.http_server.server_close()
        cls.http_server_thread.join()
        cls.proxy_server.stop()
        cls.proxy_server_thread.join()

    @staticmethod
    def make_request(request_url, proxy_server_port):
        proxy_address = 'http://127.0.0.1:%d' % proxy_server_port
        logging.info('Using proxy %s' % proxy_address)
        proxy = urllib.request.ProxyHandler({'http': proxy_address})
        opener = urllib.request.build_opener(proxy)
        urllib.request.install_opener(opener)
        logging.info('Making request to %s' % request_url)
        return urllib.request.urlopen(request_url, timeout=1)

    @unittest.skipIf(not PY3, 'Skipped for PY2 due to PY3 only make_request version')
    def test_request_via_proxied(self):
        response = TestWorkers.make_request('http://127.0.0.1:%d' % self.http_server_port, self.proxy_server_port)
        self.assertTrue(response.read(), b'OK')

    @unittest.skipIf(not PY3, 'Skipped for PY2 due to PY3 only make_request version')
    def test_502_bad_gateway(self):
        with self.assertRaises(urllib.error.HTTPError):
            TestWorkers.make_request('http://unknown.domain', self.proxy_server_port)


if __name__ == '__main__':
    unittest.main()
