// Connect: how to plug your own agents and tools into the platform — the Claude Code plugin (records every session
// and subagent, ships the MCP server), the MCP server alone (Claude Code or any MCP client), and your own data.
import { useState, type ReactNode } from 'react';
import { copyText } from '../scopeData';
import { IExternal } from '../icons';

export const REPO = 'https://github.com/leonlenk/swarm-chasing';
export const BRANCH = 'compiled-merge-v0';
const TREE = `${REPO}/tree/${BRANCH}`;

function Code({ text, label }: { text: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="code-block">
      {label && <span className="muted xs">{label}</span>}
      <div className="code-wrap">
        <pre>{text}</pre>
        <button className="btn btn-secondary btn-sm code-copy" onClick={async () => { if (await copyText(text)) { setCopied(true); setTimeout(() => setCopied(false), 1500); } }}>{copied ? 'Copied' : 'Copy'}</button>
      </div>
    </div>
  );
}

function Step({ n, title, children }: { n: number; title: string; children: ReactNode }) {
  return (
    <div className="connect-step">
      <span className="step-n">{n}</span>
      <div className="stack" style={{ gap: 8, minWidth: 0 }}><b>{title}</b>{children}</div>
    </div>
  );
}

const MCP_JSON = `{
  "mcpServers": {
    "swarm": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/swarm-chasing/swarm_mcp", "swarm-mcp"],
      "env": { "SWARM_DATA_DIR": "/path/to/swarm-chasing/data" }
    }
  }
}`;

export function Connect() {
  return (
    <div className="page-wide">
      <div className="hero">
        <div>
          <span className="hero-kicker">Integrate</span>
          <h1>Point it at your own agents.</h1>
          <p className="sub">Two ways in: the Claude Code plugin records every session and subagent you run and brings the investigation tools with it, or the MCP server alone gives any MCP client the evidence store. Needs <span className="mono">git</span>, Python 3.11+ and <a className="link" href="https://docs.astral.sh/uv/" target="_blank" rel="noreferrer">uv <IExternal /></a>.</p>
        </div>
        <div className="hero-actions">
          <a className="btn btn-secondary" href={TREE} target="_blank" rel="noreferrer">Source on GitHub <IExternal /></a>
          <a className="btn btn-primary" href={`${TREE}/swarm_mcp#readme`} target="_blank" rel="noreferrer">Docs <IExternal /></a>
        </div>
      </div>

      <div className="grid-2 connect-grid">
        <section className="card">
          <div className="card-head"><h2>Claude Code plugin</h2><span className="tag real">recommended</span></div>
          <div className="card-body stack" style={{ gap: 18 }}>
            <p className="muted small">Hooks record every session and subagent on your machine (tool calls, delegations, final answers) into a local SQLite file, and the <span className="mono">swarm</span> MCP server starts with each session. Nothing leaves your machine.</p>
            <Step n={1} title="Get the code">
              <Code text={`git clone -b ${BRANCH} ${REPO}.git ~/swarm-chasing`} />
            </Step>
            <Step n={2} title="Try it for one session, from any project">
              <Code text={'cd ~/your-project\nclaude --plugin-dir ~/swarm-chasing'} />
            </Step>
            <Step n={3} title="Or install it for good">
              <Code text={'claude plugin marketplace add ~/swarm-chasing\nclaude plugin install swarm-chasing@swarm-chasing'} />
            </Step>
            <Step n={4} title="Watch your agents here, live">
              <Code text={'cd ~/swarm-chasing\nuv run --directory swarm_mcp swarm-mcp render recall --watch &\ncd recall && npm install && npm run dev'} label="then open Live sessions" />
            </Step>
          </div>
        </section>

        <section className="card">
          <div className="card-head"><h2>MCP server only</h2><span className="tag">any MCP client</span></div>
          <div className="card-body stack" style={{ gap: 18 }}>
            <p className="muted small">Twenty-two tools over one evidence store: search, agent profiles, timelines, who-talks-to-whom graphs, recaps and notable moments, subtasks and handoffs, LLM rubric sweeps with measured precision, and findings that are rejected unless every cited id resolves.</p>
            <Step n={1} title="Add it to Claude Code">
              <Code text={'claude mcp add swarm -e SWARM_DATA_DIR=$HOME/swarm-chasing/data \\\n  -- uv run --directory $HOME/swarm-chasing/swarm_mcp swarm-mcp'} />
            </Step>
            <Step n={2} title="Or to any MCP client (Claude Desktop, Cursor, …)">
              <Code text={MCP_JSON} label="mcpServers config: replace /path/to" />
            </Step>
            <Step n={3} title="Load a dataset, then ask">
              <Code text={'uv run --directory swarm_mcp swarm-mcp add data/ai-village        # AI Village export\nuv run --directory swarm_mcp swarm-mcp add path/to/repo.git        # a repo your agents built\nuv run --directory swarm_mcp swarm-mcp add path/to/your-logs       # anything else: profiled and mapped'} />
              <p className="muted small">Start a session with <span className="mono">core_info</span> or the <span className="mono">investigate</span> prompt. Every id this UI shows (record drawer → Store id) works with <span className="mono">core_get</span> and <span className="mono">findings_record</span>.</p>
            </Step>
          </div>
        </section>
      </div>

      <section className="card">
        <div className="card-head"><h2>What each piece is</h2></div>
        <div className="card-body connect-links">
          {[
            ['Plugin manifest', '.claude-plugin/plugin.json', 'the MCP server plus the recording hooks'],
            ['Recording hooks', 'hooks/hooks.json', 'SessionStart starts the local collector; every other hook posts to it'],
            ['MCP server', 'swarm_mcp', 'SwarmScope: store, tools, findings, sweeps, export'],
            ['Adding your own data', 'swarm_mcp/ADDING_MODULES.md', 'adapters, declarative mappings and modules'],
            ['This UI', 'recall', 'replay, 43 monitors, live sessions, explorer, subtasks'],
          ].map(([name, path, what]) => (
            <a key={path} className="connect-link" href={`${REPO}/${path.includes('.') ? 'blob' : 'tree'}/${BRANCH}/${path}`} target="_blank" rel="noreferrer">
              <b>{name} <IExternal /></b><span className="mono xs">{path}</span><span className="muted small">{what}</span>
            </a>
          ))}
        </div>
      </section>
    </div>
  );
}
