import { apply as literature } from './literature.js'
import { apply as plot } from './plot.js'
import { apply as report } from './report.js'
export const name='gibbs-research'
export const inject=['tools']
export function apply(ctx,config={}){literature(ctx,config);plot(ctx,config);report(ctx,config)}
