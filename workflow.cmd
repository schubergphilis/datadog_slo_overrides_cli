#!/bin/sh
: ; exec uv run python _CI/lib/vendor/bin/invoke --search-root _CI "$@"
@uv run python _CI\lib\vendor\bin\invoke --search-root _CI %*
