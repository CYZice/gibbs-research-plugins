import ReactMarkdown from 'react-markdown';
import rehypeKatex from 'rehype-katex';
import remarkGfm from 'remark-gfm';
import remarkMath from 'remark-math';
import 'katex/dist/katex.min.css';


function wrapLegacyMathRuns(value: string): string {
  if (value.includes('$')) return value;
  const starts = /\\(?:mathsf|mathrm|mathbf|mathit|frac|sqrt)\b/g;
  let output = '';
  let cursor = 0;
  let match = starts.exec(value);
  while (match) {
    let start = match.index;
    let leading = start - 1;
    while (leading >= cursor && /\s/.test(value[leading])) leading -= 1;
    if (value[leading] === '{') start = leading;
    let index = start;
    let depth = 0;
    let sawBraces = false;
    while (index < value.length) {
      const char = value[index];
      if (char === '\\') {
        index += 1;
        while (/[A-Za-z]/.test(value[index] || '')) index += 1;
        continue;
      }
      if (char === '{') {
        depth += 1;
        sawBraces = true;
        index += 1;
        continue;
      }
      if (char === '}') {
        depth = Math.max(0, depth - 1);
        index += 1;
        continue;
      }
      if (depth > 0 || /[\s\d_.+\-–=(),^]/.test(char)) {
        index += 1;
        continue;
      }
      break;
    }
    if (!sawBraces || index <= start) {
      match = starts.exec(value);
      continue;
    }
    const run = value.slice(start, index).trimEnd();
    output += `${value.slice(cursor, start)}$${run}$`;
    cursor = start + run.length;
    starts.lastIndex = Math.max(index, cursor);
    match = starts.exec(value);
  }
  return output + value.slice(cursor);
}

interface ScientificMarkdownProps {
  children?: string | null;
  className?: string;
  inline?: boolean;
}

export default function ScientificMarkdown({
  children,
  className,
  inline = false,
}: ScientificMarkdownProps) {
  return (
    <div className={`scientific-markdown${inline ? ' inline' : ''}${className ? ` ${className}` : ''}`}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkMath]}
        rehypePlugins={[rehypeKatex]}
        components={inline ? {
          p: ({ children: content }) => <>{content}</>,
        } : undefined}
      >
        {wrapLegacyMathRuns(children || '')}
      </ReactMarkdown>
    </div>
  );
}
