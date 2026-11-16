"""Extract the original Molyte view and prepare a local, non-production demo."""
from pathlib import Path
import json
import re
import subprocess
import requests
import pymupdf

ROOT = Path(__file__).resolve().parent
MOLYTE = Path(r'D:\Microsoft VS Code\molyte_web_v2.0_resnet')
REV = '72140ea9'
targets = ['frontend/src/features/literature', 'frontend/src/pages/literature',
           'frontend/src/pages/LiteratureWorkspace.css', 'frontend/src/pages/literatureWorkspaceState.ts',
           'frontend/src/api/literatureWorkspace.ts', 'frontend/src/api/literatureLibrary.ts',
           'frontend/src/api/projects.ts', 'frontend/src/api/client.ts', 'frontend/src/api/token.ts',
           'frontend/src/utils/requestError.ts', 'frontend/src/utils/errorTranslator.ts']
files = subprocess.check_output(['git', 'ls-tree', '-r', '--name-only', REV, '--', *targets], cwd=MOLYTE).decode().splitlines()
for name in files:
    dest = ROOT / 'original' / name.removeprefix('frontend/')
    dest.parent.mkdir(parents=True, exist_ok=True)
    data = subprocess.check_output(['git', 'show', f'{REV}:{name}'], cwd=MOLYTE)
    dest.write_bytes(data)
# A truthful demo status, without changing the source repository or its UI design.
workbench = ROOT / 'original/src/features/literature/LiteratureWorkbench.tsx'
s = workbench.read_text(encoding='utf-8').replace("return '本地论文库在线'", "return '本地文献库'")
workbench.write_text(s, encoding='utf-8')

corpus = [
    ('attention','1706.03762','Attention Is All You Need',2017,'NeurIPS',
     ['Ashish Vaswani','Noam Shazeer','Niki Parmar','Jakob Uszkoreit','Llion Jones'],
     ['We propose a new simple network architecture', 'Scaled Dot-Product Attention', 'Multi-Head Attention']),
    ('bert','1810.04805','BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding',2019,'NAACL-HLT',
     ['Jacob Devlin','Ming-Wei Chang','Kenton Lee','Kristina Toutanova'],
     ['We introduce a new language representation model', 'Masked LM', 'Next Sentence Prediction']),
    ('rag','2005.11401','Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks',2020,'NeurIPS',
     ['Patrick Lewis','Ethan Perez','Aleksandra Piktus','Fabio Petroni'],
     ['We explore a general-purpose fine-tuning recipe', 'Dense Passage Retriever', 'RAG-Sequence Model']),
    ('lora','2106.09685','LoRA: Low-Rank Adaptation of Large Language Models',2022,'ICLR',
     ['Edward J. Hu','Yelong Shen','Phillip Wallis','Zeyuan Allen-Zhu'],
     ['We propose Low-Rank Adaptation', 'rank decomposition', 'trainable parameters']),
]
public = ROOT / 'public/papers'
public.mkdir(parents=True, exist_ok=True)
papers, evidence = [], []
for key, arxiv, title, year, venue, authors, needles in corpus:
    pdf = public / f'{key}.pdf'
    if not pdf.exists():
        r = requests.get(f'https://arxiv.org/pdf/{arxiv}', timeout=90)
        r.raise_for_status()
        assert r.content.startswith(b'%PDF')
        pdf.write_bytes(r.content)
    doc = pymupdf.open(pdf)
    first = doc[0].get_text()
    # Preserve actual paper text rather than generating research findings.
    abstract = first.split('Abstract', 1)[-1]
    abstract = re.split(r'\n1\s*\n|\n1\s+Introduction|\n1\s+INTRODUCTION', abstract)[0]
    abstract = re.split(r'∗Equal|\*Equal|†Work|‡Work', abstract)[0]
    abstract = re.sub(r'\s+', ' ', abstract).strip()[:2100]
    paper = dict(paper_id=key,title=title,year=year,venue=venue,arxiv_id=arxiv,
        doi=None, authors=[dict(name=n) for n in authors], abstract=abstract, open_access=True,
        source_url=f'https://arxiv.org/abs/{arxiv}',
        capabilities=dict(has_fulltext=True,can_read_content=True,can_search_evidence=True,can_download_pdf=True,has_assets=True,ai_ready=True),
        processing_status=dict(fulltext='ready',parse='ready',evidence='ready',embedding='ready',multimodal='ready'))
    papers.append(paper)
    for j,needle in enumerate(needles):
        for page in list(doc)[1:] if j else doc:
            matches = page.search_for(needle)
            if not matches:
                continue
            rect = matches[0]
            blocks = page.get_text('blocks')
            block = next((b for b in blocks if pymupdf.Rect(b[:4]).intersects(rect) and len(b[4])>90), None)
            quote = re.sub(r'\s+', ' ', block[4]).strip() if block else needle
            bbox = pymupdf.Rect(block[:4]) if block else rect
            ev = dict(evidence_id=f'{key}-e{j+1}', representation=dict(representation_id=f'{key}-paragraph-{j+1}',type='paragraph'),
                paper={k:paper[k] for k in ['paper_id','title','year','doi','venue']},
                content=dict(snippet=quote,quote=quote,quote_language='en'),
                location=dict(page=page.number+1,bbox=[bbox.x0/page.rect.width,bbox.y0/page.rect.height,bbox.x1/page.rect.width,bbox.y1/page.rect.height],coordinate_space='normalized',section_title=needle),
                modality='text',score=0.96-j*0.02)
            evidence.append(ev)
            break
    print(key, len(doc), 'pages', len([e for e in evidence if e['paper']['paper_id']==key]), 'evidence')
(ROOT/'fixtures.json').write_text(json.dumps(dict(papers=papers,evidence=evidence), ensure_ascii=False, indent=2),encoding='utf-8')
print('Extracted',len(files),'source files from',REV)
