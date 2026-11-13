# pqs-guard 家庭探针镜像：bash + curl + probe.sh，常驻循环上报
FROM alpine:3.20
RUN apk add --no-cache bash curl tzdata
COPY probe.sh /probe.sh
ENV TZ=Asia/Shanghai PROBE_LOOP=1 PROBE_PORT=58080 PROBE_INTERVAL=180 PROBE_CACHE=/tmp/pqs-probe.cache
CMD ["bash", "/probe.sh"]
