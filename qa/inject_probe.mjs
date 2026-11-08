// Does Stage Relay's resolveWorkspaceRegistry() actually return the service?
//
// Mirrors the shipped helper verbatim against the real cordis runtime, with the
// plugin's four declared dependencies provided so its apply() can run at all.
import { Context, Service } from '@deepseek-ai/cordis';

class FakeRegistry extends Service {
  constructor(ctx) { super(ctx, 'workspaceRegistry'); }
  async archiveSession(sessionId, options = {}) {
    return { archived: sessionId, stopActivity: options.stopActivity === true };
  }
}

const DEPENDENCIES = ['agents', 'sessions', 'sessionController', 'llm'];

class Dep extends Service {
  constructor(ctx, name) { super(ctx, name); }
}

// Verbatim copy of integrations/dsh/index.mjs resolveWorkspaceRegistry().
function resolveWorkspaceRegistry(ctx) {
  try { const direct = ctx.workspaceRegistry; if (direct?.archiveSession) return direct; } catch {}
  try { return ctx.reflect?.get?.('workspaceRegistry', false) ?? null; } catch { return null; }
}

async function trial(label, extraInject = []) {
  const ctx = new Context();
  ctx.plugin(FakeRegistry);
  for (const name of DEPENDENCIES) ctx.plugin(class extends Dep { constructor(c) { super(c, name); } });
  return new Promise(resolve => {
    let settled = false;
    const done = value => { if (!settled) { settled = true; resolve({ label, ...value }); } };
    ctx.plugin({
      name: 'probe-' + label,
      inject: [...DEPENDENCIES, ...extraInject],
      async apply(scope) {
        const registry = resolveWorkspaceRegistry(scope);
        let call = null;
        if (registry?.archiveSession) call = await registry.archiveSession('session-probe', { stopActivity: true });
        done({ resolved: !!registry, hasMethod: typeof registry?.archiveSession, call });
      },
    });
    setTimeout(() => done({ resolved: false, hasMethod: 'never-applied' }), 500);
  });
}

const out = [await trial('plugin-inject-list'), await trial('declared-inject', ['workspaceRegistry'])];
console.log(JSON.stringify(out, null, 2));
process.exit(out[0].resolved && out[0].call ? 0 : 3);