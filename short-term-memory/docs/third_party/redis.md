# Redis third-party runtime

short-term-memory uses Redis as an external short-term session service. It does not
vendor or modify Redis Server or redis-py source code.

## Redis Server

- Repository: https://github.com/redis/redis
- Version: `7.2.15`
- Tag commit: `316753259b4db132cf494292a1b3a702d9e9ddb2`
- Development image: `redis:7.2.15-bookworm`
- License: BSD-3-Clause

## Python client

- Repository: https://github.com/redis/redis-py
- Version: `6.4.0`
- Tag commit: `fff669daaf43ae8092ea8ab7a2a3196a9b1b7e41`
- License: MIT

## short-term-memory usage

short-term-memory uses `PING`, `RPUSH`, `LRANGE`, `LLEN`, `LTRIM`, `SET` with `EX`,
`GET`, `EXISTS`, `EXPIRE`, `DEL`, and redis-py transaction pipelines. No
upstream Redis implementation file is copied into the short-term-memory Python package.
