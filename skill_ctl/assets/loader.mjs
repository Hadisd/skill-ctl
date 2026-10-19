// Hides the "Installation scope: Project / Global" question that `npx skills add`
// asks even when skctl already passed -p, by teaching its argument parser that
// -p/--project settles the scope.
//
// This edits someone else's source, so it is written to give up rather than guess:
// an upstream that already understands -p, or one whose parser has moved, is left
// exactly as shipped and the question comes back. Switch it off permanently with
// `npx: {patch_scope_prompt: false}` in ~/.skill-ctl/config.yaml.
const MARKER = 'if (arg === "-g" || arg === "--global") options.global = true;';
const PATCH = '\n\t\telse if (arg === "-p" || arg === "--project") options.global = false;';

export async function load(url, context, nextLoad) {
  const result = await nextLoad(url, context);
  if (!url.includes('/skills/dist/cli.mjs') || !result.source) return result;

  const source = typeof result.source === 'string'
    ? result.source
    : Buffer.from(result.source).toString('utf-8');

  // Already handles the project scope itself: nothing to do.
  if (source.includes('options.global = false')) return result;

  if (!source.includes(MARKER)) {
    process.stderr.write(
      '!  skill-ctl: upstream cli.mjs changed, so it runs unpatched and may ask for the install scope.\n'
      + '   Silence this by putting `npx:` / `  patch_scope_prompt: false` in ~/.skill-ctl/config.yaml\n'
    );
    return result;
  }

  return { ...result, source: source.replace(MARKER, MARKER + PATCH) };
}
