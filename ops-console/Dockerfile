FROM docker.io/library/nginx:1.27-alpine

ARG VITE_API_BASE_URL=https://api.thedirectorate.dev
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL}

COPY nginx.conf /etc/nginx/nginx.conf
COPY public/index.html /usr/share/nginx/html/index.html.template
RUN sed "s|__API_BASE_URL__|${VITE_API_BASE_URL}|g" /usr/share/nginx/html/index.html.template \
    > /usr/share/nginx/html/index.html \
    && rm /usr/share/nginx/html/index.html.template \
    && mkdir -p /tmp/nginx/body /tmp/nginx/proxy /tmp/nginx/fastcgi /tmp/nginx/uwsgi /tmp/nginx/scgi

EXPOSE 8081
