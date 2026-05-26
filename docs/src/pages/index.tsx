import type {ReactNode} from 'react';
import Link from '@docusaurus/Link';
import useDocusaurusContext from '@docusaurus/useDocusaurusContext';
import Layout from '@theme/Layout';
import Heading from '@theme/Heading';

function HeroBanner() {
  const {siteConfig} = useDocusaurusContext();
  return (
    <header className="hero-banner">
      <div className="container">
        <img
          src="img/aifactory-logo.png"
          alt="AIFactory"
          style={{width: 96, height: 96, marginBottom: '1rem'}}
        />
        <Heading as="h1">{siteConfig.title}</Heading>
        <p className="tagline">{siteConfig.tagline}</p>
        <div className="hero-cta">
          <Link
            className="button button--secondary button--lg"
            to="/getting-started">
            Get started
          </Link>
          <Link
            className="button button--outline button--lg"
            style={{color: 'white', borderColor: 'white'}}
            to="/demo">
            Watch the demo
          </Link>
        </div>
      </div>
    </header>
  );
}

const FEATURES = [
  {
    title: 'Claude-grade planning',
    body: 'A spec-first pipeline driven by the Claude Agent SDK. Discovery → Requirements → Research → Spec → Plan → Validate, with a self-critique pass for complex work.',
  },
  {
    title: 'Multi-provider coding',
    body: 'Route each phase to the right model. Plan with Claude Opus, code with Ollama qwen3 or Codex, validate with Sonnet. Provider abstraction over Anthropic, OpenAI, Ollama, Gemini, Codex, and any OpenAI-compatible endpoint.',
  },
  {
    title: 'Live agent console',
    body: 'Watch the agent work in real time via the rmux-backed Live Console — read-only by default, one-click Attach when you want to take the keyboard. Per-task isolation via git worktrees.',
  },
];

function FeatureGrid() {
  return (
    <section className="features">
      {FEATURES.map((f) => (
        <div key={f.title} className="feature-card">
          <Heading as="h3">{f.title}</Heading>
          <p>{f.body}</p>
        </div>
      ))}
    </section>
  );
}

export default function Home(): ReactNode {
  return (
    <Layout
      title="AIFactory — Spec-Driven Development for AI agents"
      description="A web-based platform that turns GitHub issues into shipping code via a planner/coder/QA agent pipeline. Claude-grade planning, multi-provider coding, live agent console.">
      <HeroBanner />
      <main>
        <FeatureGrid />
      </main>
    </Layout>
  );
}
