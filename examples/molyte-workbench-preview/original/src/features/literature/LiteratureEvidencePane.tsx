import type { ReactNode } from 'react';

export default function LiteratureEvidencePane({
  children,
}: {
  children: ReactNode;
}) {
  return <aside className="literature-inspector">{children}</aside>;
}
