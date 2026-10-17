# assets/

One folder per platform, holding that platform's fixed artwork. The pipeline
resolves each file by **stem + extension** (`.png` first, then `.jpg`) — see
`image_pipeline/platforms.py::resolve_asset`.

```
assets/
  digi/     template.png   last.png
  alfa/     template.png   last.png
  website/  template.png   last.png      <- supplied by the owner
```

| File | Role | Required? |
| --- | --- | --- |
| `template.png` | The branded frame the product is composited into. | Yes. Missing template = that platform is skipped for the whole run, with a notice in the terminal. |
| `last.png` | The closing banner, appended after the product images. | No. Missing banner = that platform still runs, without a banner, with one notice. |

A platform whose `template.png` is absent is not an error — `resolve_platforms`
drops it and says so. Website ships without assets on purpose, so a fresh clone
runs Digimall and Alfamall only.

## Rules

- **Never** rename or move these files by hand elsewhere in the repo; the names
  above are the contract the resolver and the tests both depend on.
- The template's **logo band must never be covered** by a product image
  (`image_pipeline/AGENTS.md`). Keep the band inside the header area the
  compositing code measures. Note that the code now measures the **bottom**
  too: any ink in the lower half of a template is treated as a footer line
  (shop text, a warranty badge strip) and the product is kept above it. A
  template whose art runs from the header through the middle is read as
  having no usable footer, and keeps the plain bottom margin.
- Do not add per-product artwork here — this folder is fixed assets only.
