// Runtime for the Pin language: one translation unit made of the files in runtime/, in this order, so the
// compiler still sees (and inlines across) all of it, and the build stays one file: clang ... runtime.c.
//   core.c     allocation, requests, resources, panics
//   tasks.c    green threads, scheduler, timers, go / await
//   values.c   lists, strings, maps, JSON, objects
//   crypto.c   sha256, HMAC, password hashing, secure random numbers
//   request.c  query / form / JSON helpers, printing
//   hub.c      messages between requests, await_timeout
//   server.c   the HTTP server, headers, limits, SSE
//   client.c   the http:// client, run(), read_line()
//   sqlite.c   db_* (only with -DPIN_SQLITE)
#include "runtime/core.c"
#include "runtime/tasks.c"
#include "runtime/values.c"
#include "runtime/crypto.c"
#include "runtime/request.c"
#include "runtime/hub.c"
#include "runtime/server.c"
#include "runtime/client.c"
#include "runtime/sqlite.c"
