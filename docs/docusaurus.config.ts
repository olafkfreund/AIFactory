import {themes as prismThemes} from 'prism-react-renderer';
import type {Config} from '@docusaurus/types';
import type * as Preset from '@docusaurus/preset-classic';

// AIFactory documentation site — served at https://olafkfreund.github.io/AIFactory/

const config: Config = {
  title: 'AIFactory',
  tagline: 'Spec-Driven Development for AI agents — plan, code, ship.',
  favicon: 'img/favicon.ico',

  future: {
    v4: true,
  },

  url: 'https://olafkfreund.github.io',
  baseUrl: '/AIFactory/',

  organizationName: 'olafkfreund',
  projectName: 'AIFactory',
  deploymentBranch: 'gh-pages',
  trailingSlash: false,

  onBrokenLinks: 'warn',

  markdown: {
    mermaid: true,
    hooks: {
      onBrokenMarkdownLinks: 'warn',
    },
  },
  themes: ['@docusaurus/theme-mermaid'],

  i18n: {
    defaultLocale: 'en',
    locales: ['en'],
  },

  presets: [
    [
      'classic',
      {
        docs: {
          sidebarPath: './sidebars.ts',
          routeBasePath: '/',
          editUrl: 'https://github.com/olafkfreund/AIFactory/edit/dev/docs/',
        },
        // Blog is disabled — we don't ship one. Pages + Docs only.
        blog: false,
        theme: {
          customCss: './src/css/custom.css',
        },
      } satisfies Preset.Options,
    ],
  ],

  themeConfig: {
    image: 'img/aifactory-social-card.png',
    colorMode: {
      defaultMode: 'dark',
      respectPrefersColorScheme: true,
    },
    mermaid: {
      theme: {light: 'neutral', dark: 'dark'},
    },
    navbar: {
      title: 'AIFactory',
      logo: {
        alt: 'AIFactory',
        src: 'img/aifactory-logo.png',
      },
      items: [
        {
          type: 'docSidebar',
          sidebarId: 'mainSidebar',
          position: 'left',
          label: 'Docs',
        },
        {
          type: 'docSidebar',
          sidebarId: 'wikiSidebar',
          position: 'left',
          label: 'Wiki',
        },
        {to: '/architecture/overview', label: 'Architecture', position: 'left'},
        {to: '/showcase/', label: 'Showcase', position: 'left'},
        {
          href: 'https://github.com/olafkfreund/AIFactory',
          label: 'GitHub',
          position: 'right',
        },
      ],
    },
    footer: {
      style: 'dark',
      links: [
        {
          title: 'Docs',
          items: [
            {label: 'Getting Started', to: '/getting-started'},
            {label: 'Demo', to: '/demo'},
            {label: 'Architecture', to: '/architecture/overview'},
          ],
        },
        {
          title: 'Community',
          items: [
            {label: 'GitHub Issues', href: 'https://github.com/olafkfreund/AIFactory/issues'},
            {label: 'Discussions', href: 'https://github.com/olafkfreund/AIFactory/discussions'},
            {label: 'Contributing', to: '/contributing'},
          ],
        },
        {
          title: 'More',
          items: [
            {label: 'Roadmap', to: '/roadmap'},
            {label: 'Compliance', to: '/compliance/soc2'},
            {label: 'Changelog', href: 'https://github.com/olafkfreund/AIFactory/blob/main/CHANGELOG.md'},
          ],
        },
      ],
      copyright: `Copyright © ${new Date().getFullYear()} Olaf Krasicki-Freund and AIFactory contributors. Dual-licensed MIT OR GPL-3.0. Built with Docusaurus.`,
    },
    prism: {
      theme: prismThemes.github,
      darkTheme: prismThemes.dracula,
      additionalLanguages: ['bash', 'json', 'python', 'yaml', 'toml', 'docker'],
    },
  } satisfies Preset.ThemeConfig,
};

export default config;
