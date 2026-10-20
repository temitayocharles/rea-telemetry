.PHONY: run seed test lint docker clean

run:      ## start the server (seeds demo data on first boot)
	python3 -m craft --port 8787

seed:     ## regenerate the demo environment
	python3 -m craft seed --force

test:     ## run the stdlib test suite
	python3 -m unittest discover -s tests -v

status:   ## storage + alert summary
	python3 -m craft status

docker:   ## build and run the container
	docker compose up --build

clean:
	rm -f data/*.db data/*.db-wal data/*.db-shm
	find . -name __pycache__ -type d -exec rm -rf {} +
