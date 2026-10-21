.PHONY: help build shell api-up api-down extract psql compile estate-stats clean

help:
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*##/ —/' | column -t -s'—'

build:          ## build the images
	docker compose build lab mock-api extractor

# ---- task 1 --------------------------------------------------------------
api-up:         ## start the partner API sandbox and the warehouse
	docker compose up -d mock-api warehouse
	@echo "api:       http://localhost:8080/healthz"
	@echo "warehouse: postgresql://acme:acme@localhost:5432/acme"

api-down:       ## stop them
	docker compose stop mock-api warehouse

extract:        ## run your extractor once
	docker compose run --rm extractor

psql:           ## open a psql shell on the warehouse
	docker compose exec warehouse psql -U acme -d acme

# ---- task 2 --------------------------------------------------------------
shell:          ## interactive shell in the lab (python + sqlglot + dataform cli)
	docker compose run --rm lab bash

compile:        ## compile the Dataform project; must exit 0 before you submit
	docker compose run --rm lab sh -c 'cd task2_estate/dataform && dataform compile'

estate-stats:   ## sanity check that the estate files are readable
	docker compose run --rm lab python3 scripts/estate_stats.py

clean:          ## stop everything and drop the warehouse volume
	docker compose down --remove-orphans -v
