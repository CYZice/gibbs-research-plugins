export type PaperNode = {
  id: string;
  title: string;
  arxiv: string;
  year: number;
  x: number;
  y: number;
};

export const nodes: PaperNode[] = [
  {id:'attention',title:'Attention Is All You Need',arxiv:'1706.03762',year:2017,x:500,y:430},
  {id:'bert',title:'BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding',arxiv:'1810.04805',year:2019,x:335,y:255},
  {id:'rag',title:'Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks',arxiv:'2005.11401',year:2020,x:742,y:435},
  {id:'lora',title:'LoRA: Low-Rank Adaptation of Large Language Models',arxiv:'2106.09685',year:2022,x:455,y:665},
  {id:'transformer-xl',title:'Transformer-XL: Attentive Language Models beyond a Fixed-Length Context',arxiv:'1901.02860',year:2019,x:218,y:470},
  {id:'elmo',title:'Deep Contextualized Word Representations',arxiv:'1802.05365',year:2018,x:175,y:125},
  {id:'roberta',title:'RoBERTa: A Robustly Optimized BERT Pretraining Approach',arxiv:'1907.11692',year:2019,x:455,y:110},
  {id:'albert',title:'ALBERT: A Lite BERT for Self-supervised Learning of Language Representations',arxiv:'1909.11942',year:2020,x:615,y:235},
  {id:'bart',title:'BART: Denoising Sequence-to-Sequence Pre-training for Natural Language Generation, Translation, and Comprehension',arxiv:'1910.13461',year:2020,x:868,y:170},
  {id:'dpr',title:'Dense Passage Retrieval for Open-Domain Question Answering',arxiv:'2004.04906',year:2020,x:875,y:340},
  {id:'realm',title:'REALM: Retrieval-Augmented Language Model Pre-Training',arxiv:'2002.08909',year:2020,x:890,y:595},
  {id:'t5',title:'Exploring the Limits of Transfer Learning with a Unified Text-to-Text Transformer',arxiv:'1910.10683',year:2020,x:692,y:770},
  {id:'gpt3',title:'Language Models are Few-Shot Learners',arxiv:'2005.14165',year:2020,x:390,y:830},
  {id:'adapters',title:'Parameter-Efficient Transfer Learning for NLP',arxiv:'1902.00751',year:2019,x:175,y:735},
  {id:'prefix',title:'Prefix-Tuning: Optimizing Continuous Prompts for Generation',arxiv:'2101.00190',year:2021,x:155,y:600},
  {id:'xlnet',title:'XLNet: Generalized Autoregressive Pretraining for Language Understanding',arxiv:'1906.08237',year:2019,x:125,y:325},
  {id:'longformer',title:'Longformer: The Long-Document Transformer',arxiv:'2004.05150',year:2020,x:375,y:535},
];

// Manually arranged paper relationships for this isolated frontend preview.
export const edges: [string,string][] = [
  ['attention','bert'],['attention','rag'],['attention','lora'],['attention','transformer-xl'],
  ['bert','elmo'],['bert','roberta'],['bert','albert'],['bert','xlnet'],
  ['rag','bart'],['rag','dpr'],['rag','realm'],['rag','t5'],
  ['lora','gpt3'],['lora','adapters'],['adapters','prefix'],
  ['transformer-xl','xlnet'],['transformer-xl','longformer'],
];

export const extraNodes: PaperNode[] = [
  {id:'fid',title:'Leveraging Passage Retrieval with Generative Models for Open Domain Question Answering',arxiv:'2007.01282',year:2021,x:572,y:555},
  {id:'atlas',title:'Atlas: Few-shot Learning with Retrieval Augmented Language Models',arxiv:'2208.03299',year:2023,x:900,y:755},
];
export const extraEdges: [string,string][] = [['rag','fid'],['rag','atlas']];

export function shortenTitle(title:string, max=38):string {
  if(title.length<=max)return title;
  const words=title.split(/\s+/);
  let label=words.shift()!;
  for(const word of words){
    if((label+' '+word).length>max-3)break;
    label+=' '+word;
  }
  return label+'...';
}

export function paperLevels(points:PaperNode[], links:[string,string][]):Map<string,number>{
  const levels=new Map<string,number>([['attention',0]]);
  const queue=['attention'];
  for(let i=0;i<queue.length;i++){
    const id=queue[i];
    for(const [a,b] of links){
      const next=a===id?b:b===id?a:undefined;
      if(next&&!levels.has(next)){levels.set(next,levels.get(id)!+1);queue.push(next);}
    }
  }
  for(const n of points)if(!levels.has(n.id))levels.set(n.id,3);
  return levels;
}

export const levelStyles=[
  {radius:25,fontSize:16,color:'#4278bc'},
  {radius:17,fontSize:13.5,color:'#6e98cb'},
  {radius:10,fontSize:12,color:'#9ab6d8'},
  {radius:6,fontSize:11.5,color:'#bccde2'},
];
