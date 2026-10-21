# Offline lab image: everything needed to analyse the estate and compile Dataform,
# with no cloud credentials and no network access at run time.
FROM node:20-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip python3-venv git make jq \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"
RUN pip install --no-cache-dir \
        sqlglot==25.24.0 \
        pandas==2.2.3 \
        duckdb==1.1.3 \
        networkx==3.4.2 \
        pyyaml==6.0.2

RUN npm install -g @dataform/cli@^3.0.0

WORKDIR /work
