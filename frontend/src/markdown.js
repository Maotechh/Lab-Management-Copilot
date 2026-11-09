// Same CommonMark/GFM grammar and CJK emphasis extension as DSH ui-primitives.
import {micromark} from 'micromark';
import {gfm, gfmHtml} from 'micromark-extension-gfm';
import {cjkFriendlyStrong} from '../vendor/dsh/cjkFriendlyStrong.ts';

export function render(text) {
  // Raw HTML and dangerous protocols remain disabled by micromark. Images are
  // reduced to their captions so model text cannot contact external servers.
  const html = micromark(String(text || ''), {
    extensions: [gfm({singleTilde:false}), cjkFriendlyStrong()],
    htmlExtensions: [gfmHtml()],
  });
  const template = document.createElement('template');
  template.innerHTML = html;
  for (const image of template.content.querySelectorAll('img')) image.replaceWith(document.createTextNode(image.alt));
  for (const link of template.content.querySelectorAll('a')) {
    link.setAttribute('rel','noopener noreferrer');
    if (!link.getAttribute('href')?.startsWith('/')) link.setAttribute('target','_blank');
  }
  for (const table of template.content.querySelectorAll('table')) {
    const scroll = document.createElement('div');
    const wide=Boolean(table.querySelector('th:nth-child(4)'))&&!table.closest('blockquote');
    scroll.className='dsh-md-tableScroll '+(wide?'dsh-md-md-table-wide':'dsh-md-tableFill');
    if(wide)scroll.tabIndex=0;
    table.before(scroll); scroll.append(table);
  }
  for (const pre of template.content.querySelectorAll('pre')) {
    const block=document.createElement('div');block.className='dsh-code-block dsh-md-md-code-block';
    const banner=document.createElement('div');banner.className='dsh-code-banner';
    const language=document.createElement('span');language.className='dsh-code-infostring';
    language.textContent=pre.querySelector('code')?.className.replace(/^language-/, '')||'text';
    const copy=document.createElement('button');copy.type='button';copy.className='dsh-code-copyButton quiet';
    copy.dataset.copyCode='';copy.textContent='复制代码';
    banner.append(language,copy);pre.before(block);block.append(banner,pre);
  }
  return template.innerHTML;
}
