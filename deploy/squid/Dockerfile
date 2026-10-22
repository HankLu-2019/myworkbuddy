# Egress proxy for sandboxes (design 04 §1): CONNECT/HTTP only to allowlisted hosts, no TLS interception.
FROM debian:bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends squid ca-certificates \
 && rm -rf /var/lib/apt/lists/*
COPY squid.conf.template /etc/squid/squid.conf.template
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh && mkdir -p /etc/squid/jobs /var/log/squid \
 && chown -R proxy:proxy /var/log/squid /etc/squid/jobs
EXPOSE 3128 3129
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
