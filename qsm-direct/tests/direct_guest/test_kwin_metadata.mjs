import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../../guest/qsm_kwin_metadata.js', import.meta.url), 'utf8');
const box = (x, y, width, height) => ({x, y, width, height});
const signal = () => ({connect() {}});
const window = (id, order) => ({internalId:id, pid:42, stackingOrder:order,
    frameGeometry:box(20,30,500,400), bufferGeometry:box(20,66,500,364),
    opacity:1, minimized:false, hidden:false, desktop:1, activities:[], caption:'private-caption',
    frameGeometryChanged:signal(), damaged:signal()});
function run(workspace) {
    const messages = [];
    vm.runInNewContext(source, {workspace, callDBus(service,path,iface,method,value) {
        if (method === 'Publish') messages.push(JSON.parse(value));
    }});
    return messages.at(-1);
}
const first = window('first',9), second = window('second',1);
const legacy = {clientList:()=>[first,second], activeClient:first, currentDesktop:1,
    currentActivity:'work', virtualScreenGeometry:box(0,0,1280,800)};
let result = run(legacy);
assert.deepEqual(result.windows.map(w=>w.id), ['second','first']);
assert.deepEqual(result.windows[1].client_rect, [20,66,500,364]);
assert.equal(result.focus, 'first');
assert.ok(!JSON.stringify(result).includes('private-caption'));
result = run({...legacy,currentDesktop:2});
assert.ok(result.windows.every(w=>!w.visible)); assert.equal(result.focus,null);
first.desktop = -1;
result = run({...legacy,currentDesktop:2});
assert.equal(result.windows.find(w=>w.id==='first').visible,true);
first.activities = ['personal'];
result = run(legacy); assert.equal(result.windows.find(w=>w.id==='first').visible,false);
first.activities = [];
first.bufferGeometry = box(10,20,520,420);
result = run(legacy); assert.equal(result.windows.find(w=>w.id==='first').client_rect,null);
first.clientGeometry = box(22,66,496,362);
result = run(legacy); assert.deepEqual(result.windows.find(w=>w.id==='first').client_rect,[22,66,496,362]);
first.desktops = [{id:'a'}]; second.desktops = [{id:'b'}];
result = run({...legacy,clientList:undefined,stackingOrder:[first,second],activeWindow:second,currentDesktop:{id:'b'}});
assert.deepEqual(result.windows.map(w=>w.id), ['first','second']);
assert.equal(result.windows[0].visible,false); assert.equal(result.focus,'second');
console.log('KWin 5/6 stack, client origin, desktop/activity visibility and privacy passed');
