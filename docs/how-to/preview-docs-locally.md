# Preview docs locally

The project's documentation is a [properdocs](https://properdocs.org/) site with mkdocstrings for the API reference. One task builds it; another serves it with live reload.

## One-shot build

```bash
./workflow.cmd document
```

Runs `properdocs build` and opens the rendered `site/index.html` in your default browser. Good for a quick check.

## Live reload

```bash
uv run properdocs serve
```

Starts a local server (default `http://127.0.0.1:8000`) and watches `src/` plus `docs/` for changes. Edit a docstring or a markdown file and the browser refreshes in place.

`src/` is in the watch list because mkdocstrings reads docstrings directly — changing a Google-format docstring in `src/datadog_slo_overrides_cli/` regenerates the API reference page on the fly.

## What the build does

1. Reads `properdocs.yml` for site config, nav, and plugins.
2. The `include-markdown` plugin pulls `README.md` into `docs/index.md`.
3. The `mkdocstrings` plugin walks `src/` and renders the API reference under `docs/reference/api.md`.
4. The rest of `docs/` is rendered as plain markdown.

## Adding a page

1. Drop a new `.md` under `docs/<section>/`.
2. Add it to the `nav:` block in `properdocs.yml`.
3. Either rebuild (`./workflow.cmd document`) or rely on the live-reload server.

## Docstring style

mkdocstrings is configured for Google style (`docstring_style: google` in `properdocs.yml`). Sections it understands: `Args:`, `Returns:`, `Raises:`, `Yields:`, `Examples:`. Other formats render as plain text — useful, but you'll get less typed output and fewer cross-links.

## See also

- [Reference: API](../reference/api.md) — the page mkdocstrings populates.
- [Reference: configuration files](../reference/configuration-files.md) — `properdocs.yml` reference.
