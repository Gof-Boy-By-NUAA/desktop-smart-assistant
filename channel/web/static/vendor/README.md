# Vendor assets

Third-party frontend assets bundled locally so the Web Console can run in
fully offline / air-gapped environments (no requests to cloudflare, jsdelivr,
googleapis, gstatic, etc.).

Assets are vendored upstream releases. Do not edit minified files by hand.
Markdown is rebuilt from the locked packages as documented below; other assets
are downloaded from their listed upstream sources.

## Manifest

| Path                                                | Source                                                                                            | Version |
| --------------------------------------------------- | ------------------------------------------------------------------------------------------------- | ------- |
| `fontawesome/css/all.min.css`                       | https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css                         | 6.4.0   |
| `fontawesome/webfonts/fa-{brands,regular,solid,v4compatibility}-*.woff2` | https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/webfonts/              | 6.4.0   |
| `fonts/inter/inter-latin.woff2`                     | https://fonts.gstatic.com/s/inter/v20/UcC73FwrK3iLTeHuS_nVMrMxCp50SjIa1ZL7.woff2                  | v20     |
| `fonts/inter/inter.css`                             | Hand-written `@font-face` declaration that maps Inter weights 300-700 to the local woff2          | -       |
| `tailwind/tailwind.min.js`                          | https://cdn.tailwindcss.com (Play CDN runtime, JIT engine for the browser)                        | latest  |
| `markdown-it/markdown-it.min.js`                    | https://github.com/markdown-it/markdown-it + https://github.com/markdown-it/linkify-it; locked local build | 14.2.0 + linkify-it 5.0.2 |
| `highlightjs/highlight.min.js`                      | https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js                       | 11.9.0  |
| `highlightjs/styles/github{,-dark}.min.css`         | https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/                                | 11.9.0  |
| `highlightjs/languages/{python,javascript,java,go,bash}.min.js` | https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/languages/                  | 11.9.0  |
| `d3/d3.min.js`                                      | https://cdn.jsdelivr.net/npm/d3@7/dist/d3.min.js (loaded lazily for the knowledge graph view)     | 7.x     |

## Markdown 构建与兼容性

在 `desktop` 执行 `node scripts/build-web-markdown.cjs`；执行同一命令加
`--check` 可检查静态包与锁定依赖是否一致。构建不联网，读取当前 package-lock.json
对应的已安装依赖；`markdown-it/LICENSES.txt` 保留所有实际打包依赖的版本和许可证。

13→14 的上游变更包括 ESM 模块结构（保留 CJS）、现代浏览器要求、CommonMark
更新和图片 alt/Unicode/实体解析修复。当前 Web 通过 `window.markdownit` 使用
构造函数、core.ruler、Token 和 renderer API，没有使用 emoji 插件或内部模块路径；
这些调用方式保持不变。重建为浏览器 IIFE，保留原有全局入口，不改 console.js。
已用真实 createMd 规则比较普通 Markdown、中文链接、知识引用、文件链接和 HTML
转义等样本；这不表示所有合法或畸形输入在两个版本中都会输出完全相同的结果。

上游依据：
- https://github.com/markdown-it/markdown-it/blob/14.2.0/CHANGELOG.md
- https://github.com/markdown-it/linkify-it/security/advisories/GHSA-v245-v573-v5vm

Notes:

- The Inter font only ships the latin subset (CJK characters fall back to the
  system sans-serif via the font-family chain in `tailwind.config`).
- Only `woff2` font files are shipped (no `ttf` fallback). woff2 is supported
  by all browsers released since 2014-2018 (Chrome 36+, Firefox 39+, Safari
  12+, Edge, Opera 26+). The only mainstream browser that lacks woff2 support
  is IE 11, which cannot run the rest of the console anyway. `all.min.css`
  still references the ttf paths as a `src:` fallback — those 404s are
  harmless and ignored by the browser once the woff2 loads.
- `tailwind.min.js` is the official Tailwind Play CDN build (an in-browser JIT
  engine). It must be served as JS to keep the existing `tailwind.config = {}`
  customization working.
- One external script remains in `channel/web/static/js/console.js`:
  `wwcdn.weixin.qq.com/.../wecom-aibot-sdk` — Tencent requires the WeCom Bot
  SDK to be loaded from their CDN, and it is only fetched when the user opens
  the WeCom Bot QR-login flow.
