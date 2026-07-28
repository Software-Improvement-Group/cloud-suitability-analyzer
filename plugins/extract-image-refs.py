#!/usr/bin/env python3
"""
Pre-deployment image-ref extractor.

Walks a Flux/Helm-style deploy repo (as seen in the `integrate` and
`dunedain` customer repos) and produces one `.csa-images` file per
"app" directory, listing every distinct image ref it can resolve
statically from source (Dockerfile/manifest/HelmRelease/values.yaml
text) — no cluster access, no `helm template`, no rendering.

Two source shapes are handled, matching what both repos actually use:

1. Flux split shape (integrate: deploy/*/app/*/*-helmrelease.yaml +
   deploy/*/app/index/*/*/values.yaml; dunedain: deploy/warmind/app/warmind/
   *-helmrelease.yaml + deploy/warmind/app/index/*/*/*-values.yaml):
   repository/name/registry defaults live in the base *-helmrelease.yaml,
   the tag override lives in a separate per-environment values file. Both
   are keyed by the same dotted path down to an `image:` mapping, so we
   join base and env files per app-directory by that path.

2. In-repo chart shape (dunedain: charts/<app>/values.yaml): repository +
   tag (or a plain combined "repo:tag" string) sit together in one file
   under any `image:` key — no join needed.

Known ${VAR} placeholders (registry/domain substituted by Flux
postBuild/envsubst at reconcile time, not present in git) are preserved
literally in the output — they are not silently dropped, since a human
reviewing pre-deployment blockers needs to know the ref is unresolved
static-only and substitute the real registry before the scanner plugin
can pull it.
"""
import argparse
import os
import re
import sys

import yaml

ENV_PLACEHOLDER = re.compile(r"^\$\{[A-Z0-9_]+\}$")


def walk_images(node, path=()):
    """Yield (path, image_dict_or_str) for every 'image' key found anywhere in the YAML tree."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "image":
                yield path, v
            yield from walk_images(v, path + (k,))
    elif isinstance(node, list):
        for i, item in enumerate(node):
            yield from walk_images(item, path + (str(i),))


def load_yaml_docs(filepath):
    with open(filepath, "r", errors="replace") as f:
        try:
            return [d for d in yaml.safe_load_all(f) if d is not None]
        except yaml.YAMLError as e:
            print(f"### skip {filepath}: YAML parse error: {e}", file=sys.stderr)
            return []


def normalize_part(v):
    """image value may be a plain string ('repo:tag') or a dict (repository/tag/name/registry,
    or uchart's global.image.defaultImageRegistry/defaultImageRepository pair)."""
    if isinstance(v, str):
        return {"_combined": v}
    if isinstance(v, dict):
        keys = ("repository", "tag", "name", "registry", "defaultImageRegistry", "defaultImageRepository")
        return {k: v.get(k) for k in keys if k in v}
    return {}


def collect(filepath):
    """Return {path_tuple: {repository|tag|name|registry|_combined: value}} for one file."""
    out = {}
    for doc in load_yaml_docs(filepath):
        for path, val in walk_images(doc, ()):
            parsed = normalize_part(val)
            if parsed:
                out.setdefault(path, {}).update(parsed)
    return out


def find_global_defaults(paths):
    """Pull uchart's global.image.{defaultImageRegistry,defaultImageRepository} out of a
    collect() result, matched against templates/_image.tpl's actual composition logic
    (registry.gamewarden.io-style prefix), not just a heuristic guess."""
    for path, parts in paths.items():
        if path and path[-1] == "global" or "global" in path:
            reg = parts.get("defaultImageRegistry")
            repo = parts.get("defaultImageRepository")
            if reg or repo:
                return reg, repo
    return None, None


def merge(base, override):
    merged = {}
    for path in set(base) | set(override):
        merged[path] = {**base.get(path, {}), **override.get(path, {})}
    return merged


def render(parts, global_registry=None, global_repository=None):
    """Turn a merged image-part dict into a final image ref, reproducing uchart's
    templates/_image.tpl composition exactly (verified against /Users/brian/sourcecode/uchart):

      if image.repository is set:        ref = "{repository}:{tag}"   # fully overrides
      elif image.name is set:
        if global registry + repository: ref = "{registry}/{globalrepo}/{name}:{tag}"
        elif global registry only:       ref = "{registry}/{name}:{tag}"
        else:                            ref unresolved (chart wouldn't render one either)
    """
    if "_combined" in parts and len(parts) == 1:
        return parts["_combined"]

    tag = parts.get("tag", "latest")

    if parts.get("repository"):
        return f"{parts['repository']}:{tag}"

    name = parts.get("name")
    if name:
        registry = parts.get("registry") or global_registry
        repository = parts.get("defaultImageRepository") or global_repository
        if registry and repository:
            return f"{registry}/{repository}/{name}:{tag}"
        if registry:
            return f"{registry}/{name}:{tag}"
        return None  # matches uchart: no registry set -> template leaves $image empty

    return None


def find_files(root, patterns):
    matches = []
    for dirpath, _, filenames in os.walk(root):
        if any(seg.startswith(".") for seg in dirpath.split(os.sep)):
            continue
        for fn in filenames:
            if any(re.search(p, fn) for p in patterns):
                matches.append(os.path.join(dirpath, fn))
    return matches


def app_key(filepath):
    """Strip the -helmrelease.yaml / -values.yaml suffix to get the app's join key.
    A bare 'values.yaml' has no distinguishing stem — treat separately (single-app dir, e.g. integrate)."""
    base = os.path.basename(filepath)
    base = re.sub(r"-helmrelease\.yaml$", "", base)
    base = re.sub(r"-values\.yaml$", "", base)
    base = re.sub(r"^values\.yaml$", "", base)
    return base


def extract_flux_shape(deploy_root):
    """integrate/dunedain deploy/*/app/{<app>,index/**}/ split base+env shape.

    Base (*-helmrelease.yaml) and env override (*-values.yaml) files are paired
    by matching filename stem (e.g. berti-app-helmrelease.yaml <-> berti-app-values.yaml)
    within the same env directory — NOT a cartesian join across every base/env file
    found under deploy/, which produces false pairings once more than one app's
    files live in the same index/<env> directory (dunedain's shape).
    A bare 'values.yaml' (integrate's single-app-per-dir shape, no distinguishing
    stem) pairs with every base file found in the same base app directory instead.
    """
    results = {}
    base_files = find_files(deploy_root, [r"-helmrelease\.yaml$"])
    index_files = find_files(deploy_root, [r"-values\.yaml$", r"^values\.yaml$"])

    base_by_key = {}
    for f in base_files:
        base_by_key.setdefault(app_key(f), []).append(f)

    index_by_dir = {}
    for f in index_files:
        if "/index/" not in f and os.path.basename(os.path.dirname(f)) != "index" \
                and "/index/" not in os.path.dirname(f):
            continue
        index_by_dir.setdefault(os.path.dirname(f), []).append(f)

    for env_dir, env_files in index_by_dir.items():
        env_label = os.path.relpath(env_dir, deploy_root)
        for env_f in env_files:
            key = app_key(env_f)
            candidates = base_by_key.get(key, [])
            if not candidates and key == "":
                # bare values.yaml: single-app deploy dir — pair with whatever base file(s) exist
                candidates = [f for files in base_by_key.values() for f in files]
            if not candidates:
                print(f"### no base helmrelease match for {env_f} (key={key!r}) — tag-only, skipping", file=sys.stderr)
                continue
            env_paths = collect(env_f)
            for base_f in candidates:
                base_paths = collect(base_f)
                app_label = os.path.relpath(os.path.dirname(base_f), deploy_root)
                merged = merge(base_paths, env_paths)
                global_registry, global_repository = find_global_defaults(merged)
                refs = set()
                unresolved_names = []
                for path, parts in merged.items():
                    if path and (path[-1] == "global" or "global" in path):
                        continue  # the global.image defaults node itself, not a component
                    ref = render(parts, global_registry, global_repository)
                    if ref:
                        refs.add(ref)
                    elif parts.get("name"):
                        unresolved_names.append((path, parts))
                for path, parts in unresolved_names:
                    print(f"### {base_f}: component at {'.'.join(path)} has image.name={parts.get('name')!r} "
                          f"but no global.image.defaultImageRegistry resolvable — skipped, matches uchart's "
                          f"own template behavior (empty $image) rather than guessing", file=sys.stderr)
                if refs:
                    results.setdefault(f"{app_label}/{key or os.path.basename(base_f)} [env={env_label}]", set()).update(refs)

    return results


def extract_chart_shape(charts_root):
    """dunedain charts/<app>/values.yaml in-chart default images (no env join needed)."""
    results = {}
    value_files = find_files(charts_root, [r"^values(-[\w]+)?\.yaml$"])
    for f in value_files:
        if "/templates/" in f:
            continue
        parts_by_path = collect(f)
        refs = set()
        for path, parts in parts_by_path.items():
            ref = render(parts)
            if ref:
                refs.add(ref)
        if refs:
            app_label = os.path.relpath(os.path.dirname(f), charts_root)
            results.setdefault(f"chart:{app_label} [{os.path.basename(f)}]", set()).update(refs)
    return results


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("repo_root", help="path to the customer deploy repo")
    ap.add_argument("--out-dir", required=True, help="directory to write .csa-images files into")
    args = ap.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)

    all_results = {}
    deploy_dir = os.path.join(args.repo_root, "deploy")
    if os.path.isdir(deploy_dir):
        all_results.update(extract_flux_shape(deploy_dir))

    charts_dir = os.path.join(args.repo_root, "charts")
    if os.path.isdir(charts_dir):
        all_results.update(extract_chart_shape(charts_dir))

    if not all_results:
        print(f"### no image refs found under {args.repo_root} (checked deploy/, charts/)", file=sys.stderr)
        sys.exit(1)

    unresolved = 0
    total_refs = 0
    for label, refs in sorted(all_results.items()):
        safe_name = re.sub(r"[^a-zA-Z0-9_.-]+", "_", label).strip("_") + ".csa-images"
        out_path = os.path.join(args.out_dir, safe_name)
        with open(out_path, "w") as f:
            f.write(f"# source: {label}\n")
            for ref in sorted(refs):
                f.write(ref + "\n")
                total_refs += 1
                if any(ENV_PLACEHOLDER.match(seg) for seg in re.split(r"[/:]", ref)):
                    unresolved += 1
        print(f"{out_path}: {len(refs)} refs")

    print(f"\n{total_refs} total image refs across {len(all_results)} sources, "
          f"{unresolved} contain unresolved \\${{VAR}} placeholders (need env substitution before pull).",
          file=sys.stderr)


if __name__ == "__main__":
    main()
