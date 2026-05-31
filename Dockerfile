FROM python:3.12-slim

# Install nginx
RUN apt-get update && apt-get install -y --no-install-recommends nginx && \
    rm -rf /var/lib/apt/lists/* && \
    rm -f /etc/nginx/sites-enabled/default /etc/nginx/sites-available/default

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY fetcher.py refresh_server.py models.py auth.py web_server.py .
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY frontend/ /usr/share/nginx/html/

RUN mkdir -p /app/data /var/log/nginx /run/nginx

# Inject VERSION into sw.js template. FULL_VERSION defaults to beta tag;
# override with --build-arg FULL_VERSION_OVERRIDE="v{VERSION}" for production.
ARG FULL_VERSION_OVERRIDE=""
COPY VERSION BETA_REVISION /app/
RUN VERSION=$(cat /app/VERSION) && \
    BETA_REV=$(cat /app/BETA_REVISION) && \
    FULL_VERSION="${FULL_VERSION_OVERRIDE:-v${VERSION}-beta.${BETA_REV}}" && \
    sed -i "s/{{VERSION}}/$VERSION/g" /usr/share/nginx/html/sw.js && \
    sed -i "s/{{FULL_VERSION}}/$FULL_VERSION/g" /usr/share/nginx/html/index.html

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 80

CMD ["/entrypoint.sh"]
