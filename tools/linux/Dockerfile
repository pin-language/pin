# The Linux test machine: built once, reused for every big test round (tools/linux_test.sh).
FROM debian:stable-slim
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends clang python3 libsqlite3-dev git wrk \
        ca-certificates lsof && rm -rf /var/lib/apt/lists/*
WORKDIR /pin
