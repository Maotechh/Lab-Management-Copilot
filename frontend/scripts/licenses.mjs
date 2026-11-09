import {readFileSync,readdirSync,writeFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import path from 'node:path';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const lock=JSON.parse(readFileSync(path.join(root,'package-lock.json'),'utf8'));
const records=[];
for(const [name,entry] of Object.entries(lock.packages).sort(([a],[b])=>a.localeCompare(b))){
  if(!name||entry.dev)continue;
  const directory=path.join(root,name);
  const meta=JSON.parse(readFileSync(path.join(directory,'package.json'),'utf8'));
  const license=readdirSync(directory).find(name=>/^license(?:\.md|\.txt)?$/i.test(name));
  if(!license)throw new Error(`Missing license for ${meta.name}`);
  records.push(`${meta.name} ${meta.version} (${meta.license})\n${readFileSync(path.join(directory,license),'utf8')}\n`);
}
writeFileSync(path.join(root,'../consumable_assistant/static/dsh/THIRD-PARTY-LICENSES.txt'),records.join('\n').trimEnd()+'\n');
