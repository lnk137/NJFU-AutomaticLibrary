import {readdirSync} from 'node:fs';

// Node's direct SFC compiler does not run Vite's static asset transform.
export function transformViteAssets(code){
  const directory=new URL('../../AL-FrontEnd/static/icons/',import.meta.url);
  const icons=Object.fromEntries(readdirSync(directory).filter(name=>name.endsWith('.svg')).map(name=>['../static/icons/'+name,new URL(name,directory).href]));
  return code.replace(/import\.meta\.glob\('\.\.\/static\/icons\/\*\.svg',\{eager:true,query:'\?url',import:'default'\}\)/g,JSON.stringify(icons));
}
