import type {ReactNode} from 'react';
import Link from '@docusaurus/Link';
import useDocusaurusContext from '@docusaurus/useDocusaurusContext';
import Layout from '@theme/Layout';
import Heading from '@theme/Heading';

function HeroBanner() {
  const {siteConfig} = useDocusaurusContext();
  return (
    <header className="hero-banner">
      {/* Animated gradient orbs in the background — pure CSS, no JS */}
      <div className="hero-orbs" aria-hidden="true">
        <span className="orb orb-1" />
        <span className="orb orb-2" />
        <span className="orb orb-3" />
      </div>
      <div className="container hero-content">
        <img
          src="img/aifactory-logo.png"
          alt="AIFactory"
          className="hero-logo"
        />
        <Heading as="h1" className="hero-title">{siteConfig.title}</Heading>
        <p className="tagline">{siteConfig.tagline}</p>
        <div className="hero-meta">
          <span className="hero-badge">Self-hosted</span>
          <span className="hero-badge">27 MCP tools</span>
          <span className="hero-badge">Claude Agent SDK</span>
          <span className="hero-badge">Multi-cloud aware</span>
        </div>
        <div className="hero-cta">
          <Link
            className="button button--secondary button--lg"
            to="/getting-started">
            Get started →
          </Link>
          <Link
            className="button button--outline button--lg hero-cta-secondary"
            to="/demo">
            Watch the demo
          </Link>
        </div>
      </div>
    </header>
  );
}

// ─── Feature cards — three pillars of the platform ──────────────────────

const FEATURES = [
  {
    icon: '🧭',
    title: 'Spec-first autonomy',
    body:
      'A planner → coder → QA → human-review pipeline driven by the Claude Agent SDK. ' +
      'Plans are reviewed before code, code is reviewed before merge. Every gate is auditable.',
  },
  {
    icon: '🔀',
    title: 'Multi-provider routing',
    body:
      'Route each phase to the right model — Claude Opus for planning, Sonnet for QA, ' +
      'Ollama / Codex / Gemini / OpenAI-compatible for coding. Provider abstraction means ' +
      'no vendor lock-in.',
  },
  {
    icon: '🖥️',
    title: 'Live agent console',
    body:
      'Watch the agent work in real time via the rmux-backed Live Console — read-only by ' +
      'default, one-click Attach when you want the keyboard. Per-task git-worktree isolation.',
  },
  {
    icon: '🛰️',
    title: 'MCP control plane',
    body:
      '27 MCP tools across stdio + HTTP+SSE transports. Drive AIFactory from Claude Code, ' +
      'Cursor, Continue.dev, or any MCP-aware client. /handover skill turns "this is bigger ' +
      'than I thought" into an autonomous overnight run.',
  },
  {
    icon: '☁️',
    title: 'Infra-aware by default',
    body:
      'A catalog of default MCP servers (Kubernetes, AWS, Azure, GCP, GitHub, GitLab, ADO) ' +
      'auto-enables per project when the right markers AND credentials are detected. Read-only ' +
      'by default, audit-logged, cloud-native identity preferred (IRSA / WIF / Pod Identity).',
  },
  {
    icon: '🔐',
    title: 'Enterprise-ready foundation',
    body:
      'Self-hosted on K8s via the Helm chart. JWT auth, audit log, scope-gated MCP API keys, ' +
      'opt-in Remote Control via Claude Code\'s native --remote-control flag. OIDC, SAML+SCIM, ' +
      'and ISO 27001 evidence on the v1.1 roadmap.',
  },
];

function FeatureGrid() {
  return (
    <section className="features-section">
      <div className="features-header">
        <Heading as="h2">Built for teams that ship</Heading>
        <p className="features-subtitle">
          Six pillars. One pipeline. Your infra, your model bill, your audit trail.
        </p>
      </div>
      <div className="features">
        {FEATURES.map((f, i) => (
          <div
            key={f.title}
            className="feature-card reveal-on-scroll"
            style={{animationDelay: `${i * 80}ms`}}>
            <div className="feature-icon" aria-hidden="true">{f.icon}</div>
            <Heading as="h3">{f.title}</Heading>
            <p>{f.body}</p>
          </div>
        ))}
      </div>
    </section>
  );
}

// ─── Recently shipped — auto-running "what's new" timeline ──────────────

const RECENT = [
  {
    when: 'May 2026',
    title: 'MCP Control-Plane Epic shipped',
    body:
      '27 task-control tools across stdio + remote HTTP+SSE transports. Drive AIFactory ' +
      'from Claude Code, Cursor, or any MCP-aware client.',
    link: '/concepts/multi-provider',
    linkText: 'Read the design →',
  },
  {
    when: 'May 2026',
    title: '/handover skill — async hand-off from any chat',
    body:
      'Type /handover in Claude Code. AIFactory summarises the conversation, creates ' +
      'a spec, and runs the build while you do something else. Comes back as a draft PR.',
    link: '/concepts/multi-provider',
    linkText: 'See the workflow →',
  },
  {
    when: 'May 2026',
    title: 'Default MCP servers — Kubernetes / AWS / Azure / GitHub',
    body:
      'A catalog of well-maintained MCP servers auto-enables per project when infra ' +
      'markers + credentials line up. Read-only by default. CVE-aware version pins.',
    link: '/concepts/multi-provider',
    linkText: 'Setup guide →',
  },
  {
    when: 'May 2026',
    title: 'Remote Control — drive AIFactory tasks from your phone',
    body:
      'Wires Claude Code\'s native --remote-control flag into the agent spawn. Open ' +
      'claude.ai/code on any device and pick up where you left off.',
    link: '/concepts/remote-control',
    linkText: 'Operator guide →',
  },
];

function RecentlyShipped() {
  return (
    <section className="recent-section">
      <div className="recent-header">
        <Heading as="h2">Recently shipped</Heading>
        <p className="features-subtitle">
          The platform moves fast. Here's what landed lately.
        </p>
      </div>
      <div className="recent-timeline">
        {RECENT.map((r, i) => (
          <article
            key={r.title}
            className="recent-item reveal-on-scroll"
            style={{animationDelay: `${i * 100}ms`}}>
            <span className="recent-when">{r.when}</span>
            <Heading as="h3" className="recent-title">{r.title}</Heading>
            <p>{r.body}</p>
            <Link to={r.link} className="recent-link">{r.linkText}</Link>
          </article>
        ))}
      </div>
    </section>
  );
}

// ─── CTA strip at the bottom ───────────────────────────────────────────

function CTAStrip() {
  return (
    <section className="cta-strip">
      <div className="container">
        <Heading as="h2">Ready to try it?</Heading>
        <p>
          Self-hosted, MIT/GPL-3.0 dual-licensed. Bring your own Claude OAuth seat
          (or run it against Ollama). Zero lock-in.
        </p>
        <div className="hero-cta">
          <Link
            className="button button--secondary button--lg"
            to="/getting-started">
            Get started in 5 minutes
          </Link>
          <Link
            className="button button--outline button--lg hero-cta-secondary"
            href="https://github.com/olafkfreund/AIFactory"
            target="_blank">
            Star on GitHub ★
          </Link>
        </div>
      </div>
    </section>
  );
}

export default function Home(): ReactNode {
  return (
    <Layout
      title="AIFactory — Spec-Driven Development for AI agents"
      description="Self-hosted platform that turns ideas into shipping code via a planner/coder/QA agent pipeline. 27 MCP tools, multi-provider routing, infra-aware, enterprise-ready.">
      <HeroBanner />
      <main>
        <FeatureGrid />
        <RecentlyShipped />
        <CTAStrip />
      </main>
    </Layout>
  );
}
