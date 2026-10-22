#!/bin/sh
# Render squid.conf from the allowlists, then run Squid in the foreground. Access logs rotate daily, 30 kept.
set -eu
AGENT="${MWB_PROXY_AGENT_HOSTS:-llm-gateway pypi.org files.pythonhosted.org registry.npmjs.org proxy.golang.org sum.golang.org}"
VERIFY="${MWB_PROXY_VERIFY_HOSTS:-pypi.org files.pythonhosted.org registry.npmjs.org proxy.golang.org sum.golang.org}"
# Commas or spaces; one host per word.
AGENT=$(echo "$AGENT" | tr ',' ' ')
VERIFY=$(echo "$VERIFY" | tr ',' ' ')
case " $VERIFY " in *" llm-gateway "*) echo "MWB_PROXY_VERIFY_HOSTS must not include llm-gateway" >&2; exit 1;; esac
sed -e "s|__AGENT_HOSTS__|$AGENT|" -e "s|__VERIFY_HOSTS__|$VERIFY|" /etc/squid/squid.conf.template > /etc/squid/squid.conf
mkdir -p /etc/squid/jobs /run/squid /var/log/squid
# The include needs at least one file.
[ -e /etc/squid/jobs/00-none.conf ] || echo "# per-job ACLs follow" > /etc/squid/jobs/00-none.conf
chown -R proxy:proxy /var/log/squid /run/squid
squid -k parse -f /etc/squid/squid.conf
(while sleep 86400; do squid -k rotate 2>/dev/null || true; done) &
exec squid -N -f /etc/squid/squid.conf
