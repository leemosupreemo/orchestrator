// Usage: NODE_PATH=/path/to/node_modules node scripts/render-signin-preview.cjs
// Requires Playwright with Chromium and Python with Pillow. No live user data is used.
const {chromium} = require('playwright');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawnSync} = require('node:child_process');

(async () => {
  const frames = fs.mkdtempSync(path.join(os.tmpdir(), 'orchestrator-signin-'));
  const output = path.resolve(__dirname, '../orchestrator/web/static');
  const browser = await chromium.launch({headless: true,
    ...(process.env.CHROME_PATH ? {executablePath: process.env.CHROME_PATH} : {})});
  try {
    const page = await browser.newPage({viewport: {width:640,height:400},deviceScaleFactor:1});
    await page.goto('file://' + path.resolve(__dirname, 'signin-preview.html'));
    for (let i=0;i<120;i++) {
      await page.evaluate(t=>drawFrame(t),i/12);
      const data=await page.evaluate(()=>document.querySelector('canvas').toDataURL().split(',')[1]);
      fs.writeFileSync(path.join(frames,`${String(i).padStart(3,'0')}.png`),Buffer.from(data,'base64'));
    }
    await page.evaluate(()=>drawFrame(8.4));
    const poster=await page.evaluate(()=>document.querySelector('canvas').toDataURL().split(',')[1]);
    fs.writeFileSync(path.join(output,'signin-preview.png'),Buffer.from(poster,'base64'));
    const result=spawnSync('python3',['-c',`
from PIL import Image
from pathlib import Path
import sys
files=sorted(Path(sys.argv[1]).glob('*.png'))
frames=[Image.open(p).convert('RGB').resize((480,300),Image.Resampling.LANCZOS) for p in files]
palette=frames[100].quantize(colors=96)
frames=[f.quantize(palette=palette,dither=Image.Dither.NONE) for f in frames]
frames[0].save(sys.argv[2],save_all=True,append_images=frames[1:],duration=[80,80,90]*40,loop=0,optimize=True,disposal=1)
`,frames,path.join(output,'signin-preview.gif')],{stdio:'inherit'});
    if(result.status!==0)throw Error('GIF encoding failed');
    console.log('Rendered signin-preview.gif and signin-preview.png');
  }finally{await browser.close();fs.rmSync(frames,{recursive:true,force:true});}
})().catch(e=>{console.error(e);process.exitCode=1});
