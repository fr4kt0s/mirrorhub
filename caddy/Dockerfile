FROM caddy:2-alpine

ARG PUID=1000
ARG PGID=1000

USER root

RUN apk add --no-cache libcap \
 && if [ -n "$(getcap /usr/bin/caddy)" ]; then \
        setcap -r /usr/bin/caddy; \
    fi \
 && test -z "$(getcap /usr/bin/caddy)" \
 && addgroup -g "${PGID}" mirror \
 && adduser -D -H -u "${PUID}" -G mirror mirror

USER mirror