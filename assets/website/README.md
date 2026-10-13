# assets/website/

Empty on purpose: the Website platform's artwork is supplied by the owner and is
not committed. Drop the two files in with these exact names and the platform
starts producing output on the next run — no code change, no flag.

```
assets/website/template.png    required - the branded frame
assets/website/last.png        optional - the closing banner
```

`.jpg` is accepted as a fallback for either name. Until `template.png` exists,
Website is skipped with a notice in the terminal and no `website/` folder is
written under `images/`.

Website is the one platform that **drops** its `last.png` by default
(`website_keep_banner = false` in `image_pipeline/config.py`), so `last.png` is
only needed if that setting is turned on.
