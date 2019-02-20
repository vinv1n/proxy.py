#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
    proxy.py
    ~~~~~~~~

    HTTP Proxy Server in Python.

    :copyright: (c) 2013-2018 by Abhinav Singh.
    :license: BSD, see LICENSE for more details.
"""
import os
import sys
import errno
import queue
import base64
import socket
import select
import logging
import argparse
import datetime
import threading
import multiprocessing
from collections import namedtuple

if os.name != 'nt':
    import resource

VERSION = (0, 4)
__version__ = '.'.join(map(str, VERSION[0:2]))
__description__ = 'Lightweight HTTP, HTTPS, WebSockets Proxy Server in a single Python file'
__author__ = 'Abhinav Singh'
__author_email__ = 'mailsforabhinav@gmail.com'
__homepage__ = 'https://github.com/abhinavsingh/proxy.py'
__download_url__ = '%s/archive/master.zip' % __homepage__
__license__ = 'BSD'

logger = logging.getLogger(__name__)

PY3 = sys.version_info[0] == 3

if PY3:    # pragma: no cover
    text_type = str
    binary_type = bytes
    from urllib import parse as urlparse
else:   # pragma: no cover
    text_type = unicode
    binary_type = str
    import urlparse


def text_(s, encoding='utf-8', errors='strict'):    # pragma: no cover
    """Utility to ensure text-like usability.

    If ``s`` is an instance of ``binary_type``, return
    ``s.decode(encoding, errors)``, otherwise return ``s``"""
    if isinstance(s, binary_type):
        return s.decode(encoding, errors)
    return s


def bytes_(s, encoding='utf-8', errors='strict'):   # pragma: no cover
    """Utility to ensure binary-like usability.

    If ``s`` is an instance of ``text_type``, return
    ``s.encode(encoding, errors)``, otherwise return ``s``"""
    if isinstance(s, text_type):
        return s.encode(encoding, errors)
    return s


DEFAULT_SERVER_RECVBUF_SIZE = 1024 * 1024   # 1 Mb
DEFAULT_CLIENT_RECVBUF_SIZE = 1024 * 1024   # 1 Mb
DEFAULT_MAX_CLIENT_INACTIVITY = 30   # seconds
DEFAULT_SERVER_CONNECT_TIMEOUT = 30     # seconds
DEFAULT_LOGGING_FORMAT = '%(asctime)s - %(levelname)s - %(process)d:%(funcName)s:%(lineno)d - %(message)s'

version = bytes_(__version__)
CRLF, COLON, SP = b'\r\n', b':', b' '
PROXY_AGENT_HEADER = b'Proxy-agent: proxy.py v' + version

PROXY_TUNNEL_ESTABLISHED_RESPONSE_PKT = CRLF.join([
    b'HTTP/1.1 200 Connection established',
    PROXY_AGENT_HEADER,
    CRLF
])

BAD_GATEWAY_RESPONSE_PKT = CRLF.join([
    b'HTTP/1.1 502 Bad Gateway',
    PROXY_AGENT_HEADER,
    b'Content-Length: 11',
    b'Connection: close',
    CRLF
]) + b'Bad Gateway'

PROXY_AUTHENTICATION_REQUIRED_RESPONSE_PKT = CRLF.join([
    b'HTTP/1.1 407 Proxy Authentication Required',
    PROXY_AGENT_HEADER,
    b'Content-Length: 29',
    b'Connection: close',
    CRLF
]) + b'Proxy Authentication Required'


class ChunkParser(object):
    """HTTP chunked encoding response parser."""

    states = namedtuple('ChunkParserStates', (
        'WAITING_FOR_SIZE',
        'WAITING_FOR_DATA',
        'COMPLETE'
    ))(1, 2, 3)

    def __init__(self):
        self.state = ChunkParser.states.WAITING_FOR_SIZE
        self.body = b''     # Parsed chunks
        self.chunk = b''    # Partial chunk received
        self.size = None    # Expected size of next following chunk

    def parse(self, data):
        more = True if len(data) > 0 else False
        while more:
            more, data = self.process(data)

    def process(self, data):
        if self.state == ChunkParser.states.WAITING_FOR_SIZE:
            # Consume prior chunk in buffer
            # in case chunk size without CRLF was received
            data = self.chunk + data
            self.chunk = b''
            # Extract following chunk data size
            line, data = HttpParser.split(data)
            if not line:    # CRLF not received
                self.chunk = data
                data = b''
            else:
                self.size = int(line, 16)
                self.state = ChunkParser.states.WAITING_FOR_DATA
        elif self.state == ChunkParser.states.WAITING_FOR_DATA:
            remaining = self.size - len(self.chunk)
            self.chunk += data[:remaining]
            data = data[remaining:]
            if len(self.chunk) == self.size:
                data = data[len(CRLF):]
                self.body += self.chunk
                if self.size == 0:
                    self.state = ChunkParser.states.COMPLETE
                else:
                    self.state = ChunkParser.states.WAITING_FOR_SIZE
                self.chunk = b''
                self.size = None
        return len(data) > 0, data


class HttpParser(object):
    """HTTP request/response parser."""

    states = namedtuple('HttpParserStates', (
        'INITIALIZED',
        'LINE_RCVD',
        'RCVING_HEADERS',
        'HEADERS_COMPLETE',
        'RCVING_BODY',
        'COMPLETE'))(1, 2, 3, 4, 5, 6)

    types = namedtuple('HttpParserTypes', (
        'REQUEST_PARSER',
        'RESPONSE_PARSER'
    ))(1, 2)

    def __init__(self, parser_type):
        assert parser_type in (HttpParser.types.REQUEST_PARSER, HttpParser.types.RESPONSE_PARSER)
        self.type = parser_type
        self.state = HttpParser.states.INITIALIZED

        self.raw = b''
        self.buffer = b''

        self.headers = dict()
        self.body = None

        self.method = None
        self.url = None
        self.code = None
        self.reason = None
        self.version = None

        self.chunk_parser = None

    def is_chunked_encoded_response(self):
        return self.type == HttpParser.types.RESPONSE_PARSER and \
            b'transfer-encoding' in self.headers and \
            self.headers[b'transfer-encoding'][1].lower() == b'chunked'

    def parse(self, data):
        self.raw += data
        data = self.buffer + data
        self.buffer = b''

        more = True if len(data) > 0 else False
        while more:
            more, data = self.process(data)
        self.buffer = data

    def process(self, data):
        if self.state in (HttpParser.states.HEADERS_COMPLETE,
                          HttpParser.states.RCVING_BODY,
                          HttpParser.states.COMPLETE) and \
                (self.method == b'POST' or self.type == HttpParser.types.RESPONSE_PARSER):
            if not self.body:
                self.body = b''

            if b'content-length' in self.headers:
                self.state = HttpParser.states.RCVING_BODY
                self.body += data
                if len(self.body) >= int(self.headers[b'content-length'][1]):
                    self.state = HttpParser.states.COMPLETE
            elif self.is_chunked_encoded_response():
                if not self.chunk_parser:
                    self.chunk_parser = ChunkParser()
                self.chunk_parser.parse(data)
                if self.chunk_parser.state == ChunkParser.states.COMPLETE:
                    self.body = self.chunk_parser.body
                    self.state = HttpParser.states.COMPLETE

            return False, b''

        line, data = HttpParser.split(data)
        if line is False:
            return line, data

        if self.state == HttpParser.states.INITIALIZED:
            self.process_line(line)
        elif self.state in (HttpParser.states.LINE_RCVD, HttpParser.states.RCVING_HEADERS):
            self.process_header(line)

        # When connect request is received without a following host header
        # See `TestHttpParser.test_connect_request_without_host_header_request_parse` for details
        if self.state == HttpParser.states.LINE_RCVD and \
                self.type == HttpParser.types.REQUEST_PARSER and \
                self.method == b'CONNECT' and \
                data == CRLF:
            self.state = HttpParser.states.COMPLETE

        # When raw request has ended with \r\n\r\n and no more http headers are expected
        # See `TestHttpParser.test_request_parse_without_content_length` and
        # `TestHttpParser.test_response_parse_without_content_length` for details
        elif self.state == HttpParser.states.HEADERS_COMPLETE and \
                self.type == HttpParser.types.REQUEST_PARSER and \
                self.method != b'POST' and \
                self.raw.endswith(CRLF * 2):
            self.state = HttpParser.states.COMPLETE
        elif self.state == HttpParser.states.HEADERS_COMPLETE and \
                self.type == HttpParser.types.REQUEST_PARSER and \
                self.method == b'POST' and \
                (b'content-length' not in self.headers or
                 (b'content-length' in self.headers and
                  int(self.headers[b'content-length'][1]) == 0)) and \
                self.raw.endswith(CRLF * 2):
            self.state = HttpParser.states.COMPLETE

        return len(data) > 0, data

    def process_line(self, data):
        line = data.split(SP)
        if self.type == HttpParser.types.REQUEST_PARSER:
            self.method = line[0].upper()
            if self.method == b'CONNECT':
                self.url = urlparse.SplitResult(b'', line[1], b'', b'', b'')
            else:
                self.url = urlparse.urlsplit(line[1])
            self.version = line[2]
        else:
            self.version = line[0]
            self.code = line[1]
            self.reason = b' '.join(line[2:])
        self.state = HttpParser.states.LINE_RCVD

    def process_header(self, data):
        if len(data) == 0:
            if self.state == HttpParser.states.RCVING_HEADERS:
                self.state = HttpParser.states.HEADERS_COMPLETE
            elif self.state == HttpParser.states.LINE_RCVD:
                self.state = HttpParser.states.RCVING_HEADERS
        else:
            self.state = HttpParser.states.RCVING_HEADERS
            parts = data.split(COLON)
            key = parts[0].strip()
            value = COLON.join(parts[1:]).strip()
            self.headers[key.lower()] = (key, value)

    def build_url(self):
        if not self.url:
            return b'/None'

        url = self.url.path
        if url == b'':
            url = b'/'
        if not self.url.query == b'':
            url += b'?' + self.url.query
        if not self.url.fragment == b'':
            url += b'#' + self.url.fragment
        return url

    def build(self, del_headers=None, add_headers=None):
        req = b' '.join([self.method, self.build_url(), self.version])
        req += CRLF

        if not del_headers:
            del_headers = []
        for k in self.headers:
            if k not in del_headers:
                req += self.build_header(self.headers[k][0], self.headers[k][1]) + CRLF

        if not add_headers:
            add_headers = []
        for k in add_headers:
            req += self.build_header(k[0], k[1]) + CRLF

        req += CRLF
        if self.body:
            req += self.body

        return req

    @staticmethod
    def build_header(k, v):
        return k + b': ' + v

    @staticmethod
    def split(data):
        pos = data.find(CRLF)
        if pos == -1:
            return False, data
        line = data[:pos]
        data = data[pos + len(CRLF):]
        return line, data


class Connection(object):
    """TCP server/client connection abstraction."""

    def __init__(self, what):
        self.conn = None
        self.buffer = b''
        self.closed = True
        self.what = what  # server or client

    def __del__(self):
        if self.conn and not self.closed:
            logger.debug('closing %s connection', self.what)
            self.close()

    def send(self, data):
        # TODO: Gracefully handle BrokenPipeError exceptions
        return self.conn.send(data)

    def recv(self, bufsiz):
        try:
            data = self.conn.recv(bufsiz)
            if len(data) == 0:
                logger.debug('rcvd 0 bytes from %s' % self.what)
                return None
            logger.debug('rcvd %d bytes from %s' % (len(data), self.what))
            # logger.debug(data)
            return data
        except Exception as e:
            if e.errno == errno.ECONNRESET:
                logger.debug('%r' % e)
            else:
                logger.exception(
                    'Exception while receiving from connection %s %r with reason %r' % (self.what, self.conn, e))
            return None

    def close(self):
        self.conn.close()
        self.closed = True

    def buffer_size(self):
        return len(self.buffer)

    def has_buffer(self):
        return self.buffer_size() > 0

    def queue(self, data):
        self.buffer += data

    def flush(self):
        sent = self.send(self.buffer)
        self.buffer = self.buffer[sent:]
        logger.debug('flushed %d bytes to %s' % (sent, self.what))


class Server(Connection):
    """Establish connection to destination server."""

    def __init__(self, host, port):
        super(Server, self).__init__(b'server')
        self.addr = (host, int(port))

    def connect(self, timeout=DEFAULT_SERVER_CONNECT_TIMEOUT):
        self.conn = socket.create_connection((self.addr[0], self.addr[1]), timeout=timeout)
        self.closed = False


class Client(Connection):
    """Accepted client connection."""

    def __init__(self, conn, addr):
        super(Client, self).__init__(b'client')
        self.conn = conn
        self.closed = False
        self.addr = addr


class ProxyError(Exception):
    pass


class ProxyConnectionFailed(ProxyError):

    def __init__(self, host, port, reason):
        self.host = host
        self.port = port
        self.reason = reason

    def __str__(self):
        return '<ProxyConnectionFailed - %s:%s - %s>' % (self.host, self.port, self.reason)


class ProxyAuthenticationFailed(ProxyError):
    pass


class Proxy(threading.Thread):
    """HTTP proxy implementation.

    Accepts `Client` connection object and act as a proxy between client and server.
    """

    def __init__(self, conn, addr, auth_code=None,
                 server_connect_timeout=DEFAULT_SERVER_CONNECT_TIMEOUT,
                 server_recvbuf_size=DEFAULT_SERVER_RECVBUF_SIZE,
                 client_recvbuf_size=DEFAULT_CLIENT_RECVBUF_SIZE):
        super(Proxy, self).__init__()

        self.start_time = Proxy.now()
        self.last_activity = self.start_time

        self.auth_code = auth_code
        self.client = Client(conn, addr)
        self.client_recvbuf_size = client_recvbuf_size
        self.server = None
        self.server_recvbuf_size = server_recvbuf_size
        self.server_connect_timeout = server_connect_timeout

        self.request = HttpParser(HttpParser.types.REQUEST_PARSER)
        self.response = HttpParser(HttpParser.types.RESPONSE_PARSER)

    @staticmethod
    def get_response_pkt_by_exception(e):
        if e.__class__.__name__ == 'ProxyAuthenticationFailed':
            return PROXY_AUTHENTICATION_REQUIRED_RESPONSE_PKT
        if e.__class__.__name__ == 'ProxyConnectionFailed':
            return BAD_GATEWAY_RESPONSE_PKT

    @staticmethod
    def now():
        return datetime.datetime.utcnow()

    def inactive_for(self):
        return (Proxy.now() - self.last_activity).seconds

    def is_inactive(self):
        return self.inactive_for() > DEFAULT_MAX_CLIENT_INACTIVITY

    def verify_auth_code(self):
        if self.auth_code:
            if b'proxy-authorization' not in self.request.headers or \
                    self.request.headers[b'proxy-authorization'][1] != self.auth_code:
                raise ProxyAuthenticationFailed()

    def read_once(self):
        """Read once from client.

        :return: Data sent by client.
        None if client closed connection
        False if client is not ready for reads yet.
        """
        readable, _, _ = select.select([self.client.conn], [], [], 1)
        if self.client.conn not in readable:
            return False

        data = self.client.recv(self.client_recvbuf_size)
        self.last_activity = Proxy.now()
        if not data:
            logger.debug('client closed connection, breaking')
            return None
        
        return data

    def tunnel(self):
        """Tunnel method acts as a transparent proxy between client and server."""
        while True:
            writable = []
            if self.client.has_buffer():
                writable.append(self.client.conn)
            if not self.server.closed and self.server.has_buffer():
                writable.append(self.server.conn)

            # Always read from client and server to detect connection close events
            readable, writable, _ = select.select([self.client.conn, self.server.conn], writable, [], 1)

            # Flush client and server buffers
            if self.client.conn in writable:
                self.last_activity = Proxy.now()
                self.client.flush()
            if self.server.conn in writable:
                self.server.flush()

            # Buffer client and server data for respective ends
            if self.client.conn in readable:
                data = self.client.recv(self.client_recvbuf_size)
                self.last_activity = Proxy.now()
                if not data:
                    logger.debug('client closed connection, breaking')
                    break
                self.server.queue(data)
            if self.server.conn in readable:
                data = self.server.recv(self.server_recvbuf_size)
                if not data:
                    logger.debug('server closed connection, breaking')
                    break
                self.response.parse(data)
                self.client.queue(data)

    def proxy_https_request(self):
        """Handles https protocol proxy.

        Establish connection with the remote server and
        return PROXY_TUNNEL_ESTABLISHED_RESPONSE_PKT to the client.
        From here on, transparently act as a tunnel between client and server.
        """
        host, port = self.request.url.netloc.split(COLON)
        self.server = Server(host, port)
        try:
            self.server.connect(self.server_connect_timeout)
            logger.debug('connected to server %s:%s' % (host, port))
        except (TimeoutError, socket.gaierror) as e:
            raise ProxyConnectionFailed(host, port, repr(e))

        self.client.queue(PROXY_TUNNEL_ESTABLISHED_RESPONSE_PKT)

        self.tunnel()

    def proxy_http_request(self):
        """Establish connection with the remote server and
        proxy client request to remote server.

        Going forward:
        --------------
        We will stream incoming request from the client to the server,
        instead of buffering entire request from client in memory.
        This can also lead to scenarios where remote server can close the
        connection prematurely without waiting to receive the entire request.

        For Http requests, request packet is parsed by
        HttpParser. As the request gets parsed, header-by-header
        and then chunk-by-chunk of body is progressively streamed
        to the remote server, while also listening for response from
        the remote server.

        If remote server accepts the whole request, identified by
        HttpParser.states.COMPLETE, server response is again parsed
        by HttpParser and streamed to the client.
        """
        host, port = self.request.url.hostname, self.request.url.port if self.request.url.port else 80
        self.server = Server(host, port)
        try:
            self.server.connect(self.server_connect_timeout)
            logger.debug('connected to server %s:%s' % (host, port))
        except (TimeoutError, socket.gaierror) as e:
            raise ProxyConnectionFailed(host, port, repr(e))

        while self.request.state != HttpParser.states.COMPLETE:
            data = self.read_once()
            if data is None:
                return
            self.request.parse(data)

        self.server.queue(self.request.build(
            del_headers=[b'proxy-authorization', b'proxy-connection', b'connection', b'keep-alive'],
            add_headers=[(b'Via', b'1.1 proxy.py v%s' % version), (b'Connection', b'Close')]
        ))

        self.tunnel()

    def handle_server_request(self):
        while self.request.state != HttpParser.states.COMPLETE:
            data = self.read_once()
            if data is None:
                break
            self.request.parse(data)

        self.client.queue(b'HTTP/1.1 200 OK\r\nContent-Length:8\r\n\r\nproxy.py')
        while self.client.has_buffer():
            _, writable, _ = select.select([], [self.client.conn], [], 1)
            if self.client.conn in writable:
                self.client.flush()

    def process(self):
        """Read client request method line.

        We need to inspect request line to differentiate between
        http vs https requests. Also, identify requests made to
        the proxy.py web server itself.
        """
        handler = None

        while True:
            data = self.read_once()
            if data is None:
                break

            self.request.parse(data)

            if self.request.state >= HttpParser.states.LINE_RCVD:
                if self.request.method == b'CONNECT':
                    handler = self.proxy_https_request
                elif self.request.url:
                    if self.request.url.netloc != b'':
                        handler = self.proxy_http_request
                    else:
                        handler = self.handle_server_request
                else:
                    raise Exception('Invalid request\n%s' % self.request.raw)
                break

        if handler:
            handler()

    def access_log(self):
        host, port = self.server.addr if self.server else (None, None)
        if self.request.method == b'CONNECT':
            logger.info(
                '%s:%s - %s %s:%s' % (self.client.addr[0], self.client.addr[1], self.request.method, host, port))
        elif self.request.method:
            logger.info('%s:%s - %s %s:%s%s - %s %s - %s bytes' % (
                self.client.addr[0], self.client.addr[1], self.request.method, host, port, self.request.build_url(),
                self.response.code, self.response.reason, len(self.response.raw)))

    def run(self):
        logger.debug('Proxying connection %r' % self.client.conn)
        try:
            self.process()
        except KeyboardInterrupt:
            pass
        except Exception as e:
            logger.exception('Exception while handling connection %r with reason %r' % (self.client.conn, e))
        finally:
            logger.debug(
                'closing client connection with pending client buffer size %d bytes, server buffer size %d bytes' %
                (self.client.buffer_size(), self.server.buffer_size() if self.server else 0))
            if self.client and not self.client.closed:
                self.client.close()
            if self.server and not self.server.closed:
                self.server.close()
            self.access_log()
            logger.debug('Closing proxy for connection %r at address %r' % (self.client.conn, self.client.addr))


class TCP(object):
    """TCP server implementation.

    Subclass MUST implement `handle` method. It accepts an instance of accepted `Client` connection.
    """

    def __init__(self, hostname='127.0.0.1', port=8899, backlog=100):
        self.running = False
        self.hostname = hostname
        self.port = port
        self.backlog = backlog
        self.socket = None

    def handle(self, conn, addr):
        raise NotImplementedError()

    def shutdown(self):
        pass

    def run(self):
        self.running = True
        try:
            logger.info('Starting proxy.py on port %d' % self.port)
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.socket.bind((self.hostname, self.port))
            self.socket.listen(self.backlog)

            while self.running:
                readable, _, _ = select.select([self.socket], [], [], 1)
                if self.socket in readable:
                    conn, addr = self.socket.accept()
                    self.handle(conn, addr)
        except Exception as e:
            logger.exception('Exception while running the server %r' % e)
        finally:
            self.shutdown()
            self.running = False
            self.socket.close()
            logger.info('Closed server socket')

    def stop(self):
        self.running = False


class HTTP(TCP):
    """HTTP proxy server implementation.

    Dispatches accepted client connections over the `client_queue`. Requests
    are consumed from the queue and proxied by the worker processes.
    """

    def __init__(self, hostname='127.0.0.1', port=8899, backlog=100, num_workers=0,
                 server_connect_timeout=DEFAULT_SERVER_CONNECT_TIMEOUT,
                 auth_code=None, server_recvbuf_size=DEFAULT_SERVER_RECVBUF_SIZE,
                 client_recvbuf_size=DEFAULT_CLIENT_RECVBUF_SIZE):
        super(HTTP, self).__init__(hostname, port, backlog)
        self.auth_code = auth_code
        self.client_recvbuf_size = client_recvbuf_size
        self.server_recvbuf_size = server_recvbuf_size
        self.server_connect_timeout = server_connect_timeout

        self.client_queue = multiprocessing.Queue()

        self.num_workers = multiprocessing.cpu_count()
        if num_workers > 0:
            self.num_workers = num_workers
        self.workers = []

    def init_workers(self):
        logger.info('Starting %d workers' % self.num_workers)
        for worker_id in range(self.num_workers):
            worker = Worker(self.client_queue)
            worker.daemon = True
            worker.start()
            self.workers.append(worker)

    def handle(self, conn, addr):
        self.client_queue.put((Worker.operations.PROXY, {
            'conn': conn,
            'addr': addr,
            'auth_code': self.auth_code,
            'server_connect_timeout': self.server_connect_timeout,
            'server_recvbuf_size': self.server_recvbuf_size,
            'client_recvbuf_size': self.client_recvbuf_size,
        }))

    def shutdown(self):
        logger.info('Shutting down workers')
        for worker_id in range(self.num_workers):
            self.client_queue.put((Worker.operations.SHUTDOWN, {}))
        for worker_id in range(self.num_workers):
            self.workers[worker_id].join()

    def run(self):
        self.init_workers()
        super(HTTP, self).run()


class Worker(multiprocessing.Process):
    """Proxy worker runs in a separate processes
    and spawns Proxy threads for received client request.

    Accepted client requests are queued over `client_queue`
    by the main thread. One of the worker picks up the client
    request and spawns a separate thread per to proxy the request.

    Workers allow proxy.py to take advantage of all the cores
    available on the machine.
    """

    operations = namedtuple('WorkerOperations', (
        'SHUTDOWN',
        'PROXY'
    ))(1, 2)

    def __init__(self, client_queue):
        super(Worker, self).__init__()
        self.client_queue = client_queue

    @staticmethod
    def proxy(conn, addr, auth_code=None, server_connect_timeout=DEFAULT_SERVER_CONNECT_TIMEOUT,
              server_recvbuf_size=DEFAULT_SERVER_RECVBUF_SIZE,
              client_recvbuf_size=DEFAULT_CLIENT_RECVBUF_SIZE):
        p = Proxy(conn, addr, auth_code=auth_code,
                  server_connect_timeout=server_connect_timeout,
                  server_recvbuf_size=server_recvbuf_size,
                  client_recvbuf_size=client_recvbuf_size)
        p.daemon = True
        p.start()

    def run(self):
        while True:
            try:
                operation, payload = self.client_queue.get(True, 1)
                if operation == Worker.operations.SHUTDOWN:
                    break
                elif operation == Worker.operations.PROXY:
                    Worker.proxy(payload['conn'],
                                 payload['addr'],
                                 payload['auth_code'],
                                 payload['server_recvbuf_size'],
                                 payload['client_recvbuf_size'])
            except queue.Empty:
                pass
            # Safeguard against https://gist.github.com/abhinavsingh/b8d4266ff4f38b6057f9c50075e8cd75
            except ConnectionRefusedError:
                pass
            except KeyboardInterrupt:
                break


def set_open_file_limit(soft_limit):
    """Configure open file description soft limit on supported OS."""
    if os.name != 'nt':  # resource module not available on Windows OS
        curr_soft_limit, curr_hard_limit = resource.getrlimit(resource.RLIMIT_NOFILE)
        if curr_soft_limit < soft_limit < curr_hard_limit:
            resource.setrlimit(resource.RLIMIT_NOFILE, (soft_limit, curr_hard_limit))
            logger.info('Open file descriptor soft limit set to %d' % soft_limit)


def main():
    parser = argparse.ArgumentParser(
        description='proxy.py v%s' % __version__,
        epilog='Having difficulty using proxy.py? Report at: %s/issues/new' % __homepage__
    )

    parser.add_argument('--hostname', type=str, default='127.0.0.1', help='Default: 127.0.0.1')
    parser.add_argument('--port', type=int, default=8899, help='Default: 8899')
    parser.add_argument('--backlog', type=int, default=100,
                        help='Default: 100. '
                             'Maximum number of pending connections '
                             'to proxy server.')
    parser.add_argument('--num-workers', type=int, default=0, help='Default: Number of CPU cores.')
    parser.add_argument('--server-connect-timeout', type=int, default=DEFAULT_SERVER_CONNECT_TIMEOUT,
                        help='Default: 30 seconds. Timeout when connecting to upstream servers.')
    parser.add_argument('--basic-auth', type=str, default=None,
                        help='Default: No authentication. '
                             'Specify colon separated user:password '
                             'to enable basic authentication.')
    parser.add_argument('--server-recvbuf-size', type=int, default=DEFAULT_SERVER_RECVBUF_SIZE,
                        help='Default: 1 MB. '
                             'Maximum amount of data received from the '
                             'server in a single recv() operation. Bump this '
                             'value for faster downloads at the expense of '
                             'increased RAM.')
    parser.add_argument('--client-recvbuf-size', type=int, default=DEFAULT_CLIENT_RECVBUF_SIZE,
                        help='Default: 1 MB. '
                             'Maximum amount of data received from the '
                             'client in a single recv() operation. Bump this '
                             'value for faster uploads at the expense of '
                             'increased RAM.')
    parser.add_argument('--open-file-limit', type=int, default=1024,
                        help='Default: 1024. '
                             'Maximum number of files (TCP connections) '
                             'that proxy.py can open concurrently.')
    parser.add_argument('--log-level', type=str, default='INFO',
                        help='DEBUG, INFO (default), WARNING, ERROR, CRITICAL.')
    parser.add_argument('--version', action='store_true', help='Print proxy.py version.')
    args = parser.parse_args()

    logging.basicConfig(level=getattr(logging, args.log_level.upper()), format=DEFAULT_LOGGING_FORMAT)

    if args.version:
        print(text_(version))
        sys.exit(0)

    try:
        set_open_file_limit(args.open_file_limit)

        auth_code = None
        if args.basic_auth:
            auth_code = b'Basic %s' % base64.b64encode(bytes_(args.basic_auth))

        proxy = HTTP(hostname=args.hostname,
                     port=args.port,
                     backlog=args.backlog,
                     num_workers=args.num_workers,
                     server_connect_timeout=args.server_connect_timeout,
                     auth_code=auth_code,
                     server_recvbuf_size=args.server_recvbuf_size,
                     client_recvbuf_size=args.client_recvbuf_size)
        proxy.run()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
