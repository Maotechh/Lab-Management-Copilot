# DSH web components for the laboratory website

The reference is the server's DSH web client at `127.0.0.1:3080`, not the macOS desktop application. Its client source is unchanged from official commit `fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159` in the deployed DSH revision `a51d6a6df84930fb8d4aa0a9daf26523984b7560`.

`consumable_assistant/static/dsh` contains the upstream theme, typography, elevation, corner and scrollbar files without modification. The selected SVG artwork, Markdown and code-block CSS, and CJK emphasis parser are derived directly from that commit. `source.json` records upstream paths and SHA-256 values. The upstream MIT license and browser dependency licenses are distributed alongside these files.

The laboratory shell adapts `ui-layout`, `ui-sidebar`, `ui-conversation`, `ui-chat` and `ui-primitives` to the existing FastAPI application. It follows the 280/56-pixel sidebar, 38-pixel new-session action, 36/28-pixel buttons, 28-pixel composer radius, 34-pixel send action, message width/spacing and centered new-session layout. Touch controls have larger interaction areas. Settings, forms and confirmation panels use the same semantic colors, in both light and dark themes.

This does not run the general DSH developer interface inside the teacher website. DSH remains a separate loopback service. Shared conversation messages, tasks and tool completions come from persisted server records. The server retains business previews, confirmation, version checks, ordering and deduplication. The browser keeps the existing DOM nodes when updating history and rejects responses older than the latest event number.

Markdown uses the same CommonMark/GFM grammar and the upstream CJK emphasis extension. The DOM adapter applies the upstream Markdown/code/table styles, copies original text, disables raw HTML and dangerous protocols, and renders image captions without fetching model-specified images. DSH's privileged local-file navigation, general settings and plugin installation are not exposed.

## Rebuild

Use the pinned upstream checkout and the committed npm lockfile:

```sh
python scripts/vendor_dsh_ui.py --source /path/to/deepseek-harness
npm ci --prefix frontend --ignore-scripts --no-audit --no-fund
npm run build --prefix frontend
```

Commit generated browser files together with source/lockfile changes. Deployments serve these files directly and need no browser build or package registry at runtime.

## Browser verification

Use an isolated application database. `check_dsh_interface.cjs` substitutes only the chat endpoints with deterministic fixtures and checks layout, Markdown, copy, drafts, confirmation states, reconnection, late responses, cancellation, duplicate creation, dark mode, drawer resizing and touch/recording controls. It performs no production writes. `check_chat_history.cjs` checks 201-message pagination and retained DOM/state. `browser_check.cjs` exercises actual business APIs and must only target an isolated application.

```sh
PLAYWRIGHT_MODULE=/path/to/playwright BROWSER_CHANNEL=chrome TEST_URL=http://127.0.0.1:8039 node scripts/check_dsh_interface.cjs
PLAYWRIGHT_MODULE=/path/to/playwright BROWSER_CHANNEL=chrome TEST_URL=http://127.0.0.1:8039 node scripts/check_chat_history.cjs
```

Viewport and touch emulation do not establish actual iPhone/Android recording, certificate trust or locked-screen push delivery.
