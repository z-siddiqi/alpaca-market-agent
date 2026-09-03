FROM node:24-bookworm-slim AS firebase

RUN npm install --global firebase-tools@15.28.2

FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=firebase /usr/local/bin/node /usr/local/bin/node
COPY --from=firebase /usr/local/lib/node_modules/firebase-tools /usr/local/lib/node_modules/firebase-tools
RUN ln -s /usr/local/lib/node_modules/firebase-tools/lib/bin/firebase.js /usr/local/bin/firebase

WORKDIR /app

COPY deploy/web/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY .firebaserc firebase.json ./
COPY web ./web

CMD ["python", "web/render.py", "--all", "--publish"]
