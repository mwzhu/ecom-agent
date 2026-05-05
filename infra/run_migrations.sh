#!/bin/sh
set -eu

exec uv run alembic -c infra/alembic.ini upgrade head
