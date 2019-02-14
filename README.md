proxy.py
========

Lightweight HTTP, HTTPS and WebSockets Proxy Server in Python.

![alt text](https://travis-ci.org/abhinavsingh/proxy.py.svg?branch=develop "Build Status")

Features
--------

- Distributed as a single file module
- No external dependency other than standard Python library
- Support for `http`, `https` and `websockets` request proxy
- Optimize for large file uploads and downloads
- IPv4 and IPv6 support
- Basic authentication support

Install
-------

To install proxy.py, simply:

	$ pip install --upgrade proxy.py

Using docker:

    $ docker run -it -p 8899:8899 --rm abhinavsingh/proxy.py

Usage
-----

```
$ proxy.py -h
usage: proxy.py [-h] [--hostname HOSTNAME] [--port PORT] [--backlog BACKLOG]
                [--num-workers NUM_WORKERS]
                [--server-connect-timeout SERVER_CONNECT_TIMEOUT]
                [--basic-auth BASIC_AUTH]
                [--server-recvbuf-size SERVER_RECVBUF_SIZE]
                [--client-recvbuf-size CLIENT_RECVBUF_SIZE]
                [--open-file-limit OPEN_FILE_LIMIT] [--log-level LOG_LEVEL]
                [--version VERSION]

proxy.py v0.4

optional arguments:
  -h, --help            show this help message and exit
  --hostname HOSTNAME   Default: 127.0.0.1
  --port PORT           Default: 8899
  --backlog BACKLOG     Default: 100. Maximum number of pending connections to
                        proxy server.
  --num-workers NUM_WORKERS
                        Default: Number of CPU cores.
  --server-connect-timeout SERVER_CONNECT_TIMEOUT
                        Default: 30 seconds. Timeout when connecting to
                        upstream servers.
  --basic-auth BASIC_AUTH
                        Default: No authentication. Specify colon separated
                        user:password to enable basic authentication.
  --server-recvbuf-size SERVER_RECVBUF_SIZE
                        Default: 1 MB. Maximum amount of data received from
                        the server in a single recv() operation. Bump this
                        value for faster downloads at the expense of increased
                        RAM.
  --client-recvbuf-size CLIENT_RECVBUF_SIZE
                        Default: 1 MB. Maximum amount of data received from
                        the client in a single recv() operation. Bump this
                        value for faster uploads at the expense of increased
                        RAM.
  --open-file-limit OPEN_FILE_LIMIT
                        Default: 1024. Maximum number of files (TCP
                        connections) that proxy.py can open concurrently.
  --log-level LOG_LEVEL
                        DEBUG, INFO (default), WARNING, ERROR, CRITICAL.
  --version VERSION     Print proxy.py version.

Having difficulty using proxy.py? Report at:
https://github.com/abhinavsingh/proxy.py/issues/new
```
