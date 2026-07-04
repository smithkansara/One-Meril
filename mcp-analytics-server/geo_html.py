"""Self-contained D3 + TopoJSON renderer for the adaptive geo tool.

`build_geo_result` (geo_analysis.py) decides WHAT to draw and returns a decision
dict; this module turns that dict into ONE self-contained HTML page:

  * choropleth  — world countries (world-atlas) or US states (us-atlas)
  * bubble      — hub-city / point bubbles on a world basemap
  * table       — sortable fallback when a map would mislead

Every rendered feature is clickable (Step 6 drill-down): the side panel shows the
full ranked entity list, the sample size n, and the field(s) used. Sequential vs
categorical color follows the decision (Step 3). Low-n locations are visually
de-emphasized rather than shown as confidently as well-sampled ones (Step 5). A
"why this visual" panel + a warning list make the fallbacks and any dropped
locations explicit (OUTPUT CONTRACT).

D3 v7, topojson-client v3 and the topology JSON are loaded from jsdelivr — the same
CDN the existing Chart.js widgets use, so browsers rendering the LibreChat iframe
can reach them. The page reports its own height via `ui-size-change` so the mcp-ui
host grows the iframe to fit (no internal scrollbar), matching the analytics widget.
"""
import re
import json

_GEO_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<script src="https://cdn.jsdelivr.net/npm/d3@7"></script>
<script src="https://cdn.jsdelivr.net/npm/topojson-client@3"></script>
<style>
:root{
  --surface:#fcfcfb;--plane:#f4f1ea;--ink:#17140d;--ink2:#52514e;
  --line:#ddd6c5;--accent:#b9542b;--bad:#a83a2f;--warn:#8a6d00;
  --land:#e7e2d6;--landstroke:#cfc8b6;--panel:#ffffff;
}
@media (prefers-color-scheme:dark){
  :root{--surface:#1a1a19;--plane:#0d0d0d;--ink:#f2f0e9;--ink2:#c3c2b7;
    --line:#2c2c2a;--accent:#e07a4f;--bad:#e0705f;--warn:#e0c65f;
    --land:#2a2a28;--landstroke:#3a3a37;--panel:#222220;}
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{overflow:hidden}
body{background:transparent;color:var(--ink);padding:4px;
  font-family:system-ui,-apple-system,"Segoe UI",sans-serif;line-height:1.5}
.card{background:var(--surface);border:1px solid var(--line);border-radius:10px;
  padding:16px 18px;margin-bottom:12px}
.gtitle{font-size:21px;font-weight:700;margin:0 0 4px}
.why{font-size:13.5px;color:var(--ink2)}
.why b{color:var(--ink)}
.meta{display:flex;flex-wrap:wrap;gap:8px 18px;margin-top:10px;font-size:12px;color:var(--ink2)}
.meta .k{color:var(--ink);font-weight:600}
.chip{display:inline-block;background:var(--plane);border:1px solid var(--line);
  border-radius:999px;padding:2px 10px;font-size:11.5px;font-weight:600;color:var(--ink)}
.stage{display:flex;gap:16px;align-items:flex-start;flex-wrap:wrap}
.mapwrap{flex:1 1 380px;min-width:280px}
svg.map{width:100%;height:auto;display:block;background:transparent;border-radius:8px}
.land{fill:var(--land);stroke:var(--landstroke);stroke-width:.4}
.feat{cursor:pointer;stroke:var(--surface);stroke-width:.5;transition:opacity .15s}
.feat.lown{stroke-dasharray:2 2;stroke:var(--warn);stroke-width:.8}
.feat.sel{stroke:var(--ink);stroke-width:1.6}
.feat:hover{opacity:.82}
.bub{cursor:pointer;stroke:var(--surface);stroke-width:.8;fill-opacity:.78}
.bub.lown{stroke:var(--warn);stroke-dasharray:2 2;fill-opacity:.45}
.bub.sel{stroke:var(--ink);stroke-width:2}
.legend{margin-top:10px;font-size:12px;color:var(--ink2)}
.legend .row{display:flex;align-items:center;gap:7px;margin:3px 0}
.sw{width:13px;height:13px;border-radius:3px;flex:0 0 auto;border:1px solid rgba(0,0,0,.15)}
.gradbar{height:11px;border-radius:6px;margin:5px 0 3px}
.gradlab{display:flex;justify-content:space-between;font-variant-numeric:tabular-nums}
.lown-key{margin-top:8px;font-size:11.5px;color:var(--warn)}
/* drill panel */
.drill{flex:1 1 240px;min-width:230px;max-width:340px;background:var(--panel);
  border:1px solid var(--line);border-radius:10px;padding:14px 16px;font-size:13px;
  align-self:stretch}
.drill h3{font-size:15px;margin:0 0 2px;display:flex;justify-content:space-between;align-items:center}
.drill .x{cursor:pointer;color:var(--ink2);font-weight:700;font-size:16px;line-height:1;border:none;background:none}
.drill .sub{color:var(--ink2);font-size:11.5px;margin-bottom:10px}
.drill .kv{display:flex;justify-content:space-between;gap:10px;padding:4px 0;border-bottom:1px dashed var(--line)}
.drill .kv .k{color:var(--ink2)}
.drill .kv .v{font-weight:600;font-variant-numeric:tabular-nums;text-align:right}
.drill .rank{margin:10px 0 2px;font-size:11px;letter-spacing:1px;text-transform:uppercase;color:var(--accent);font-weight:700}
.drill ol{margin:4px 0 0 0;padding:0;list-style:none;counter-reset:r}
.drill ol li{counter-increment:r;display:flex;justify-content:space-between;gap:8px;
  padding:4px 0;border-bottom:1px dashed var(--line);font-variant-numeric:tabular-nums}
.drill ol li::before{content:counter(r);color:var(--ink2);margin-right:8px;min-width:14px;display:inline-block}
.drill ol li .nm{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.drill .win{color:var(--accent);font-weight:700}
.drill .hint{color:var(--ink2);font-style:italic}
.drill .lowflag{color:var(--warn);font-weight:600;margin-top:8px}
/* table */
table.geo{width:100%;border-collapse:collapse;font-size:13px}
table.geo th,table.geo td{padding:8px 10px;text-align:left;border-bottom:1px solid var(--line)}
table.geo th{cursor:pointer;user-select:none;color:var(--ink2);font-size:11px;
  letter-spacing:.5px;text-transform:uppercase;white-space:nowrap}
table.geo th:hover{color:var(--ink)}
table.geo th .ar{opacity:.5;font-size:10px}
table.geo td.num{text-align:right;font-variant-numeric:tabular-nums}
table.geo tr.clk{cursor:pointer}
table.geo tr.clk:hover{background:var(--plane)}
table.geo tr.lown td{color:var(--warn)}
table.geo tr.lown td:first-child::after{content:" ⚠";font-size:11px}
table.geo tr.sel td{background:var(--plane)}
/* warnings */
.warns{border-color:var(--warn)}
.warns .wlabel{font-size:11px;letter-spacing:1.4px;text-transform:uppercase;
  color:var(--warn);font-weight:700;margin-bottom:6px}
.warns ul{margin:0 0 0 18px;font-size:12.5px;color:var(--ink2)}
.warns li{margin:4px 0}
.note{font-size:12px;color:var(--ink2);margin-top:6px}
.loading{padding:30px;text-align:center;color:var(--ink2);font-size:13px}
.err{color:var(--bad);font-size:13px;padding:8px 0}
</style>
</head>
<body>
<div id="root"></div>
<script>
(function(){
var GEO = __GEO__;
var TITLE = __TITLE__;

// ---- iframe auto-resize (mcp-ui host grows to fit; no internal scrollbar) ----
function sendSize(){
  try{
    var h=Math.max(document.documentElement.scrollHeight, document.body.scrollHeight)+6;
    parent.postMessage({type:"ui-size-change",payload:{height:h}},"*");
  }catch(e){}
}
window.addEventListener("load",function(){
  try{parent.postMessage({type:"ui-lifecycle-iframe-ready"},"*");}catch(e){}
  sendSize();
});
if(window.ResizeObserver){new ResizeObserver(sendSize).observe(document.body);}
window.addEventListener("resize",sendSize);
setInterval(sendSize,1500);

var CATS=["#4e79a7","#f28e2b","#e15759","#76b7b2","#59a14f","#edc948","#b07aa1","#ff9da7","#9c755f","#bab0ac"];
var VB_W=960, VB_H=500;

function esc(s){return String(s==null?"":s)
  .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");}
function fmt(v){
  if(v==null||isNaN(v))return "—";
  var a=Math.abs(v);
  if(a>=1e9)return (v/1e9).toFixed(2)+"B";
  if(a>=1e6)return (v/1e6).toFixed(2)+"M";
  if(a>=1e3)return (v/1e3).toFixed(1)+"K";
  return (Math.round(v*100)/100).toLocaleString();
}

// If the CDN libraries didn't load (offline / blocked), fail with a readable
// message instead of a blank frame — d3 is needed before anything can render.
if(typeof d3==="undefined"){
  document.getElementById("root").innerHTML='<div class="card err">'
    +'The interactive map libraries could not be loaded (no internet access to the CDN). '
    +'Try again on a connected network.</div>';
  sendSize();
  return;
}
if(typeof topojson==="undefined"){
  // topojson is only needed for choropleths; degrade zone/city/table gracefully.
  window.topojson={feature:function(){return {type:"FeatureCollection",features:[]};}};
}

var root=d3.select("#root");

// ---------- decision / output-contract header ----------
function header(){
  var c=root.append("div").attr("class","card");
  c.append("div").attr("class","gtitle").text(TITLE||"Geographic analysis");
  c.append("div").attr("class","why").html("<b>"+esc(visualName())+"</b> — "+esc(GEO.why||""));
  var m=c.append("div").attr("class","meta");
  var g=GEO.granularity||{};
  if(g.level){m.append("span").html('<span class="k">Granularity:</span> <span class="chip">'
      +esc(g.level)+(g.confidence!=null?" · "+Math.round(g.confidence*100)+"%":"")+'</span>');}
  if(GEO.topology&&GEO.topology.url){
    var src=GEO.topology.url.split("/").pop();
    m.append("span").html('<span class="k">Boundary source:</span> '+esc(src));
  } else if(GEO.visual==="table"){
    m.append("span").html('<span class="k">Boundary source:</span> none (table)');
  }
  var vf=GEO.value_field, gf=GEO.group_field;
  m.append("span").html('<span class="k">Value:</span> '+esc(vf||"—")
      +(gf?' &nbsp;<span class="k">Category:</span> '+esc(gf):''));
  if(GEO.currency_note){c.append("div").attr("class","note").text("💱 "+GEO.currency_note);}
}
function visualName(){
  return {choropleth:"Choropleth map",bubble:"Hub / point bubble map",
          table:"Sortable table (map withheld)",none:"No visual"}[GEO.visual]||GEO.visual;
}

// ---------- warnings (nothing disappears silently) ----------
function warnings(){
  var w=GEO.warnings||[];
  if(!w.length)return;
  var c=root.append("div").attr("class","card warns");
  c.append("div").attr("class","wlabel").text("⚠ Warnings — "+w.length+" item"+(w.length>1?"s":""));
  var ul=c.append("ul");
  w.forEach(function(t){ul.append("li").text(t);});
}

// ---------- color scale ----------
function buildColor(feats){
  var enc=GEO.color_encoding||"sequential";
  if(enc==="categorical"){
    var winners=[];
    feats.forEach(function(f){if(f.winner&&winners.indexOf(f.winner)<0)winners.push(f.winner);});
    var s=d3.scaleOrdinal().domain(winners).range(CATS);
    return {kind:"categorical", of:function(f){return f.winner?s(f.winner):"#bbb";},
            domain:winners, scale:s};
  }
  var vals=feats.map(function(f){return f.value;}).filter(function(v){return v!=null&&!isNaN(v);});
  var lo=d3.min(vals), hi=d3.max(vals);
  if(lo===hi){hi=lo+1;}
  var s=d3.scaleSequential(d3.interpolateBlues).domain([lo,hi]);
  return {kind:"sequential", of:function(f){return f.value==null?"#bbb":s(f.value);},
          lo:lo, hi:hi, scale:s};
}

function legend(container,color){
  var lg=container.append("div").attr("class","legend");
  if(color.kind==="categorical"){
    lg.append("div").style("font-weight","600").style("color","var(--ink)")
      .text("Winning "+(GEO.group_field||"category")+" per location");
    color.domain.forEach(function(d){
      var r=lg.append("div").attr("class","row");
      r.append("span").attr("class","sw").style("background",color.scale(d));
      r.append("span").text(d);
    });
  } else {
    lg.append("div").style("font-weight","600").style("color","var(--ink)")
      .text((GEO.value_field||"Value")+" (low → high)");
    var stops=[];
    for(var i=0;i<=10;i++){stops.push(color.scale(color.lo+(color.hi-color.lo)*i/10));}
    lg.append("div").attr("class","gradbar")
      .style("background","linear-gradient(90deg,"+stops.join(",")+")");
    var gl=lg.append("div").attr("class","gradlab");
    gl.append("span").text(fmt(color.lo));
    gl.append("span").text(fmt(color.hi));
  }
  lg.append("div").attr("class","lown-key")
    .text("⚠ dashed / faded = low sample size (n below threshold) — read with caution");
}

// ---------- drill-down panel (Step 6) ----------
var drillSel=null;
function drill(panel,f,clearSel){
  if(drillSel)drillSel.classed("sel",false);
  if(clearSel)drillSel=clearSel;
  panel.html("");
  panel.append("h3").html('<span>'+esc(f.label||f.location)+'</span>')
       .append("button").attr("class","x").attr("title","clear").text("×")
       .on("click",function(){panel.html("");if(drillSel)drillSel.classed("sel",false);
         panel.append("div").attr("class","hint").text("Click a location to inspect it.");sendSize();});
  panel.append("div").attr("class","sub").text("Verify the number isn't one overweighted point.");
  var kv=function(k,v){var d=panel.append("div").attr("class","kv");
    d.append("span").attr("class","k").text(k);d.append("span").attr("class","v").text(v);};
  kv(GEO.value_field||"Value", fmt(f.value));
  kv("Sample size (n)", f.n);
  if(f.rep)kv("Hub city (approx.)", f.rep);
  if(GEO.group_field)kv("Field(s) used", (GEO.value_field||"value")+" by "+GEO.group_field);
  else kv("Field used", GEO.value_field||"value");
  if(f.lowN)panel.append("div").attr("class","lowflag")
      .text("⚠ Low sample size (n="+f.n+") — this color/size is not statistically solid.");
  var ranked=f.ranked||[];
  if(ranked.length){
    panel.append("div").attr("class","rank").text("Ranked "+(GEO.group_field||"entities")+" here");
    var ol=panel.append("ol");
    ranked.forEach(function(e,i){
      var li=ol.append("li").classed("win",i===0&&GEO.color_encoding==="categorical");
      li.append("span").attr("class","nm").text(e.name);
      li.append("span").text(fmt(e.value));
    });
  } else {
    panel.append("div").attr("class","hint").style("margin-top","8px")
      .text("No per-category breakdown (no group field provided).");
  }
  sendSize();
}

// ---------- map plumbing ----------
function projectionFor(name){
  if(name==="geoAlbersUsa")return d3.geoAlbersUsa();
  if(name==="geoMercator")return d3.geoMercator();
  return d3.geoNaturalEarth1();
}

function renderMapShell(){
  var stage=root.append("div").attr("class","stage");
  var mapwrap=stage.append("div").attr("class","mapwrap");
  var svg=mapwrap.append("svg").attr("class","map")
    .attr("viewBox","0 0 "+VB_W+" "+VB_H).attr("preserveAspectRatio","xMidYMid meet");
  var status=mapwrap.append("div").attr("class","loading").text("Loading map…");
  var panel=stage.append("div").attr("class","drill");
  panel.append("div").attr("class","hint").text("Click a location to inspect it.");
  return {mapwrap:mapwrap, svg:svg, status:status, panel:panel};
}

function fitToFeatures(proj,geoFeats){
  if(proj.fitExtent && geoFeats.length){
    try{proj.fitExtent([[24,20],[VB_W-24,VB_H-20]],
        {type:"FeatureCollection",features:geoFeats});return;}catch(e){}
  }
  if(proj.fitSize)proj.fitSize([VB_W,VB_H],{type:"Sphere"});
}

// ---------- choropleth ----------
function renderChoropleth(){
  var ui=renderMapShell();
  var color=buildColor(GEO.features);
  var byKey={};
  GEO.features.forEach(function(f){byKey[f.key]=f;});
  d3.json(GEO.topology.url).then(function(topo){
    ui.status.remove();
    var objName=GEO.topology.object;
    var obj=topo.objects[objName]||topo.objects[Object.keys(topo.objects)[0]];
    var geo=topojson.feature(topo,obj);
    var all=geo.features;
    var matched=all.filter(function(d){return byKey[(d.properties&&d.properties.name)]; });
    var proj=projectionFor(GEO.topology.projection);
    if(GEO.topology.projection==="geoAlbersUsa"){proj.fitSize([VB_W,VB_H],geo);}
    else{fitToFeatures(proj, matched.length?matched:all);}
    var path=d3.geoPath(proj);
    var svg=ui.svg;
    // basemap (context)
    svg.append("g").selectAll("path.land").data(all).enter().append("path")
      .attr("class","land").attr("d",path);
    // data features
    svg.append("g").selectAll("path.feat")
      .data(matched).enter().append("path")
      .attr("class",function(d){var f=byKey[d.properties.name];return "feat"+(f&&f.lowN?" lown":"");})
      .attr("d",path)
      .attr("fill",function(d){return color.of(byKey[d.properties.name]);})
      .attr("fill-opacity",function(d){return byKey[d.properties.name].lowN?0.45:1;})
      .append("title").text(function(d){var f=byKey[d.properties.name];
        return f.label+": "+fmt(f.value)+" (n="+f.n+")"+(f.lowN?" ⚠ low n":"");});
    svg.selectAll("path.feat").on("click",function(ev,d){
      var sel=d3.select(this);drill(ui.panel,byKey[d.properties.name],sel);sel.classed("sel",true);});
    legend(ui.mapwrap,color);
    sendSize();
  }).catch(function(e){
    ui.status.attr("class","err").text("Could not load boundary topology ("+esc(GEO.topology.url)+"). "
      +"Showing the table instead.");
    renderTableInto(root,true);sendSize();
  });
}

// ---------- bubble ----------
function renderBubble(){
  var ui=renderMapShell();
  var color=buildColor(GEO.features);
  var vals=GEO.features.map(function(f){return f.value;}).filter(function(v){return v!=null;});
  var rmax=d3.max(vals.map(Math.abs))||1;
  var r=d3.scaleSqrt().domain([0,rmax]).range([3,34]);
  d3.json(GEO.topology.url).then(function(topo){
    ui.status.remove();
    var obj=topo.objects[GEO.topology.object]||topo.objects[Object.keys(topo.objects)[0]];
    var geo=topojson.feature(topo,obj);
    var proj=projectionFor(GEO.topology.projection);
    // auto-zoom to the data extent (correct zoom automatically)
    var pts={type:"FeatureCollection",features:GEO.features.map(function(f){
      return {type:"Feature",geometry:{type:"Point",coordinates:[f.lon,f.lat]}};})};
    if(proj.fitExtent && GEO.features.length){
      try{proj.fitExtent([[40,40],[VB_W-40,VB_H-40]],
          GEO.features.length>1?pts:geo);}catch(e){proj.fitSize([VB_W,VB_H],geo);}
    } else {proj.fitSize([VB_W,VB_H],geo);}
    var path=d3.geoPath(proj);
    var svg=ui.svg;
    svg.append("g").selectAll("path.land").data(geo.features).enter().append("path")
      .attr("class","land").attr("d",path);
    var g=svg.append("g");
    // larger bubbles first so small ones stay clickable on top
    var feats=GEO.features.slice().sort(function(a,b){return Math.abs(b.value)-Math.abs(a.value);});
    g.selectAll("circle.bub").data(feats).enter().append("circle")
      .attr("class",function(f){return "bub"+(f.lowN?" lown":"");})
      .attr("cx",function(f){var p=proj([f.lon,f.lat]);return p?p[0]:-99;})
      .attr("cy",function(f){var p=proj([f.lon,f.lat]);return p?p[1]:-99;})
      .attr("r",function(f){return r(Math.abs(f.value)||0);})
      .attr("fill",function(f){return color.of(f);})
      .attr("display",function(f){return proj([f.lon,f.lat])?null:"none";})
      .on("click",function(ev,f){var sel=d3.select(this);drill(ui.panel,f,sel);sel.classed("sel",true);})
      .append("title").text(function(f){return f.label+(f.rep?" ("+f.rep+")":"")
        +": "+fmt(f.value)+" (n="+f.n+")"+(f.lowN?" ⚠ low n":"");});
    legend(ui.mapwrap,color);
    ui.mapwrap.select(".legend").append("div").attr("class","lown-key")
      .style("color","var(--ink2)").text("Bubble size = magnitude of "+(GEO.value_field||"value")+".");
    sendSize();
  }).catch(function(e){
    ui.status.attr("class","err").text("Could not load basemap topology. Showing the table instead.");
    renderTableInto(root,true);sendSize();
  });
}

// ---------- table ----------
function renderTableInto(container,asFallback){
  var c=container.append("div").attr("class","card");
  if(asFallback){c.append("div").attr("class","note")
    .text("Rendered as a table — see the reason above.");}
  var rows=(GEO.features||[]).map(function(f){
    return {location:f.label||f.location, value:f.value, n:f.n, lowN:f.lowN,
            winner:f.winner, ranked:f.ranked};});
  var cols=[{k:"location",t:"Location",num:false},
            {k:"value",t:(GEO.value_field||"Value"),num:true},
            {k:"n",t:"n",num:true}];
  if(GEO.group_field)cols.push({k:"winner",t:"Top "+GEO.group_field,num:false});
  var stage=c.append("div").attr("class","stage");
  var tw=stage.append("div").style("flex","1 1 380px").style("min-width","280px");
  var tbl=tw.append("table").attr("class","geo");
  var panel=stage.append("div").attr("class","drill");
  panel.append("div").attr("class","hint").text("Click a row to inspect it.");
  var sortK="value", sortDir=-1;
  var thead=tbl.append("thead").append("tr");
  cols.forEach(function(col){
    thead.append("th").html(esc(col.t)+' <span class="ar"></span>')
      .on("click",function(){sortDir=(sortK===col.k?-sortDir:(col.num?-1:1));sortK=col.k;draw();});
  });
  var tbody=tbl.append("tbody");
  function draw(){
    thead.selectAll("th .ar").text(function(d,i){
      return cols[i].k===sortK?(sortDir<0?"▼":"▲"):"";});
    rows.sort(function(a,b){
      var x=a[sortK],y=b[sortK];
      if(typeof x==="number"||typeof y==="number"){x=+x||0;y=+y||0;return (x-y)*sortDir;}
      return String(x||"").localeCompare(String(y||""))*sortDir;});
    var tr=tbody.selectAll("tr").data(rows,function(d){return d.location;});
    tr.exit().remove();
    var e=tr.enter().append("tr").attr("class","clk");
    e.merge(tr).attr("class",function(d){return "clk"+(d.lowN?" lown":"");})
      .html("").each(function(d){
        var row=d3.select(this);
        cols.forEach(function(col){
          var v=d[col.k];
          row.append("td").attr("class",col.num?"num":null)
            .text(col.num&&col.k==="value"?fmt(v):(v==null?"—":v));
        });
      })
      .on("click",function(ev,d){
        tbody.selectAll("tr").classed("sel",false);d3.select(this).classed("sel",true);
        drill(panel,d,null);});
    sendSize();
  }
  draw();
}

// ---------- boot ----------
function boot(){
  header();
  if(GEO.visual==="choropleth")renderChoropleth();
  else if(GEO.visual==="bubble")renderBubble();
  else if(GEO.visual==="table")renderTableInto(root,false);
  else if(GEO.visual==="none"){
    root.append("div").attr("class","card err")
      .text(GEO.why||"No renderable geographic data.");
  }
  warnings();
  sendSize();
}
try{
  boot();
}catch(err){
  try{
    root.append("div").attr("class","card err")
      .text("The geographic view hit an unexpected error while rendering: "
            +((err&&err.message)||err)+". The underlying analysis is unaffected.");
  }catch(e2){
    document.getElementById("root").innerHTML=
      '<div class="card err">The geographic view could not be rendered.</div>';
  }
  sendSize();
}
})();
</script>
</body>
</html>"""


def make_geo_html(result: dict, title: str = "Geographic analysis") -> str:
    """Render a geo decision dict (from build_geo_result) into a self-contained page.

    The JSON is embedded inside a <script> tag, so `</` is escaped to `<\\/` — a
    location value containing "</script>" then cannot break out of the block. The
    sequence is valid inside a JS string literal and decodes back to the original."""
    geo_json = json.dumps(result, separators=(",", ":")).replace("</", "<\\/")
    title_json = json.dumps(title or "Geographic analysis").replace("</", "<\\/")
    # Single-pass substitution over the TEMPLATE only. A chained
    # `.replace("__GEO__", …).replace("__TITLE__", …)` would re-scan the already-
    # inserted JSON, so a data value that literally contains "__TITLE__"/"__GEO__"
    # would be clobbered and break the embedded `var GEO` literal (blank widget).
    # re.sub with a callback never re-scans the replacement text.
    subs = {"__GEO__": geo_json, "__TITLE__": title_json}
    return re.sub(r"__GEO__|__TITLE__", lambda m: subs[m.group(0)], _GEO_HTML)
