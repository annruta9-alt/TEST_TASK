FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

# Apache + mod_wsgi come from the same distribution as the system Python 3.12, so the
# module and the interpreter always match. ffmpeg is used by the zoom step (apply_zoom.py).
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        apache2 libapache2-mod-wsgi-py3 python3 python3-venv ffmpeg curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Python dependencies in a virtualenv (mod_wsgi is pointed at it with python-home)
RUN python3 -m venv /opt/venv
COPY requirements.txt /app/requirements.txt
RUN /opt/venv/bin/pip install --no-cache-dir -r /app/requirements.txt

COPY web_service.py db.py apply_zoom.py worker.py app.wsgi /app/
COPY apache/video.conf /etc/apache2/sites-available/video.conf
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN a2dissite 000-default \
 && a2ensite video \
 && sed -i 's#^ErrorLog .*#ErrorLog /dev/stderr#' /etc/apache2/apache2.conf \
 && mkdir -p /app/uploads \
 && chown -R www-data:www-data /app/uploads \
 && sed -i 's/\r$//' /usr/local/bin/docker-entrypoint.sh \
 && chmod +x /usr/local/bin/docker-entrypoint.sh \
 && ffmpeg -hide_banner -encoders 2>/dev/null | grep -q libvpx-vp9

EXPOSE 80
# default: the web server; the `worker` service overrides this command
CMD ["/usr/local/bin/docker-entrypoint.sh"]
