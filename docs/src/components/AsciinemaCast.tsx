/**
 * Tiny wrapper around the asciinema-player React component so we can
 * embed `.cast` recordings from MDX showcase pages.
 *
 * Plays only on the client (Docusaurus uses SSR/SSG by default).
 * If the asciinema-player CDN bundle isn't available at runtime, the
 * placeholder text below is what the user sees — better than a
 * broken element.
 */

import React from 'react';
import BrowserOnly from '@docusaurus/BrowserOnly';

interface Props {
  src: string;
  title?: string;
  speed?: number;
  idleTimeLimit?: number;
}

export default function AsciinemaCast({
  src,
  title,
  speed = 1.5,
  idleTimeLimit = 2,
}: Props): JSX.Element {
  return (
    <BrowserOnly fallback={<div>Loading recording…</div>}>
      {() => {
        // CDN-loaded player. Lazy import so SSR doesn't try to bundle it.
        // The asciinema-player package exposes `create(url, container, opts)`.
        const containerRef = React.useRef<HTMLDivElement>(null);

        React.useEffect(() => {
          let cancelled = false;
          (async () => {
            try {
              // @ts-expect-error — runtime-only ESM import from CDN
              const mod = await import(
                /* webpackIgnore: true */
                'https://cdn.jsdelivr.net/npm/asciinema-player@3.10.0/dist/bundle/asciinema-player.min.js'
              );
              if (cancelled || !containerRef.current) return;
              mod.create(src, containerRef.current, {
                speed,
                idleTimeLimit,
                fit: 'width',
                terminalFontSize: '14px',
                title,
              });
            } catch (err) {
              console.error('AsciinemaCast: failed to load player', err);
            }
          })();
          return () => {
            cancelled = true;
          };
        }, []);

        return (
          <div>
            <link
              rel="stylesheet"
              href="https://cdn.jsdelivr.net/npm/asciinema-player@3.10.0/dist/bundle/asciinema-player.min.css"
            />
            <div ref={containerRef} style={{maxWidth: '100%'}} />
          </div>
        );
      }}
    </BrowserOnly>
  );
}
