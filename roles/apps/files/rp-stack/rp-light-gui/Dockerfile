FROM nginx:1.27-alpine

COPY rp-light-gui/nginx.conf /etc/nginx/conf.d/default.conf
COPY rp-light-gui/index.html /usr/share/nginx/html/index.html
COPY rp-light-gui/styles.css /usr/share/nginx/html/styles.css
COPY rp-light-gui/app.js /usr/share/nginx/html/app.js
EXPOSE 80
