import { defineConfig } from 'vite';
import uni from '@dcloudio/vite-plugin-uni';
import { readFileSync } from 'node:fs';

function mailCancellationAssets(){
  return {name:'mail-cancellation-assets',generateBundle(){
    for(const name of ['mail-cancel.html','mail-cancel.js']){
      this.emitFile({type:'asset',fileName:'static/'+name,source:readFileSync(new URL('./static/'+name,import.meta.url),'utf8')});
    }
  }};
}

export default defineConfig({
  plugins: [uni(),mailCancellationAssets()],
});
