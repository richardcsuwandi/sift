<p align="center">
  <img src="app/static/icon.svg" alt="Sift app icon" width="120" height="120">
</p>

<h1 align="center">Sift</h1>

<p align="center">
  <strong>Find, understand, and organize your files with a private local AI.</strong>
</p>

<p align="center">
  <a href="LICENSE"><img alt="MIT license" src="https://img.shields.io/badge/license-MIT-blue.svg"></a>
  <img alt="macOS" src="https://img.shields.io/badge/platform-macOS-111111?logo=apple">
  <a href="https://ollama.com/"><img alt="Powered by Ollama" src="https://img.shields.io/badge/AI-local%20with%20Ollama-000000"></a>
</p>

Sift is a personal file copilot for macOS. Ask for a file in natural language,
search inside documents and images, or describe how a folder should be cleaned
up. Sift turns every change into an editable plan before touching anything on
disk—and it runs entirely on your machine.

<p align="center">
  <img src="demo/organize.webp" alt="Sift organizing a folder into a clean structure" width="900">
</p>

## What you can do

### Find files by meaning

Ask the way you remember: “the paper about learning from a few examples” or
“the invoice from March.” Sift searches filenames and document contents, then
shows the matching evidence so you can verify every result.

<p align="center">
  <img src="demo/semantic_retrieval.webp" alt="Finding a document with semantic search in Sift" width="900">
</p>

### Search inside screenshots and images

Opt into image reading during a scan and Sift can find screenshots by visible
text and visual context—not just by an unhelpful filename.

<p align="center">
  <img src="demo/vision_retrieval.webp" alt="Finding a screenshot by its visual contents in Sift" width="900">
</p>

### Organize a folder as one coherent collection

Sift looks at the full batch and proposes useful categories, subfolders, and
clearer filenames together. That keeps related files together instead of
classifying each item into an inconsistent one-off folder.

<p align="center">
  <img src="demo/organize.webp" alt="Reviewing an AI-generated folder organization plan in Sift" width="900">
</p>

### Move, rename, or trash files in plain English

Say “move all PDFs into Papers,” “rename these papers as
author_year_title,” or “trash the installers.” Every instruction becomes the
same reviewable before-and-after plan.

<p align="center">
  <img src="demo/move.webp" alt="Moving files with a natural-language instruction in Sift" width="900">
</p>

### Stay in control

- Review the complete before-and-after tree before anything moves.
- Edit individual destinations, skip files, or apply only selected changes.
- Undo the last applied batch, including files moved to the Trash.
- Avoid overwrites with automatic collision-safe names.
- Keep credentials and system folders out of scope with built-in path guards.

## Why Sift

Sift sits between file search, rule-based automation, and AI organization. It
is designed for the moment when you know what a file *means* but not what it is
called—or know the outcome you want without wanting to build a permanent rule.

| | **Sift** | **[Hazel](https://www.noodlesoft.com/)** | **[DropIt](https://www.dropitproject.com/)** |
|---|---|---|---|
| Primary interaction | Ask questions or give plain-language instructions | Build conditions and actions for watched folders | Build filter-and-action associations |
| Best suited to | Understanding, finding, and interactively reorganizing mixed files | Powerful ongoing macOS automation | Repeatable Windows file-processing workflows |
| Organization | Proposes a structure from the contents of the current batch | Follows the rules and destinations you define | Follows the profiles and destinations you define |
| Search experience | Natural-language search across filenames, documents, and indexed images, with visible evidence | Uses file attributes and content as automation conditions | Uses names, properties, and content as action filters |
| Change control | Editable plan, selective apply, collision protection, and one-click undo | Rule preview and configurable automated actions | Manual or monitored processing through configured actions |
| AI and privacy | Your choice of local Ollama models; file contents stay on your Mac | Local rules-based Mac app | Local open-source Windows utility |

Hazel and DropIt are excellent choices when you already know the enduring
rules you want to automate. Sift is for exploratory retrieval and
content-aware cleanup: understand the batch, decide what should happen, review
the proposal, then apply it.

## Supported content

Sift reads filenames and common document formats, including PDF, Word,
PowerPoint, Excel, plain text, Markdown, source code, LaTeX, and BibTeX. With a
local vision model, it can also index PNG, JPEG, GIF, BMP, WebP, TIFF, and HEIC
images.

## Get started

### Requirements

- macOS
- Python 3.11 or newer
- [Ollama](https://ollama.com/download)

### Install and run

```bash
git clone https://github.com/richardcsuwandi/sift.git
cd sift

ollama pull qwen3:4b
ollama pull qwen3-embedding:0.6b

./run.sh
```

Sift opens at [http://127.0.0.1:8000](http://127.0.0.1:8000). The first run
creates an isolated Python environment and installs the required packages.

For image search, install a vision model before launching:

```bash
ollama pull qwen2.5vl:3b
```

The model pickers automatically list compatible models already installed in
Ollama. `qwen3:4b`, `qwen3-embedding:0.6b`, and `qwen2.5vl:3b` are defaults,
not lock-ins.

### Optional macOS launcher

After running Sift once, build a convenient local launcher:

```bash
./macos/build_app.sh
```

Open `dist/Sift.app` whenever you want to start Sift. The launcher remains
connected to this repository, so keep the folder in place.

## Privacy and safety

Sift does not upload filenames, document text, images, prompts, or embeddings.
The web interface binds to localhost, models run through your local Ollama
installation, and the search index stays in the ignored `data/` directory.

Sift never changes a file during scanning. Organize and instruction results
remain proposals until you approve them. “Delete” requests move items to the
macOS Trash rather than permanently erasing them, and the most recent batch can
be undone from the app.

## Demo files

The original full-resolution recordings are available in [`demo/`](demo/).
The animated previews above are optimized copies for fast rendering on GitHub.

## License

[MIT](LICENSE) © Richard Cornelius Suwandi.

Built by [Richard Cornelius Suwandi](https://richardcsuwandi.github.io).
