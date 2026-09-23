// piboy-settings — menu de reglages plein ecran (Wayland layer-shell) pour le
// PiBoy DMG / Batocera. Lance depuis le systeme ES "Reglages systeme" ; le
// launcher gele EmulationStation le temps du menu. Navigation a la manette
// (lue en evdev), valeurs modifiees en direct dans les fichiers de config
// (piboy-osd.conf / piboy-led.conf / piboy-fan.conf), que l'OSD et le daemon
// rechargent automatiquement.
#define _GNU_SOURCE
#include <wayland-client.h>
#include "wlr-layer-shell-unstable-v1-client-protocol.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <math.h>
#include <poll.h>
#include <dirent.h>
#include <sys/mman.h>
#include <sys/ioctl.h>
#include <linux/input.h>

#define STB_TRUETYPE_IMPLEMENTATION
#include "stb_truetype.h"

#include <stdio.h>
#define RES_DIR "/userdata/system/piboy-osd/resources"
#define OSD_CONF "/userdata/system/piboy-osd.conf"
#define LED_CONF "/userdata/system/piboy-led.conf"
#define FAN_CONF "/userdata/system/piboy-fan.conf"
#define PWR_CONF "/userdata/system/piboy-power.conf"
// kinds d'item
enum { K_KEY=0, K_LEDCOLOR=1, K_WIFI=2, K_BT=3, K_INFO=4 };

// ---------------------------------------------------------------- wayland ---
static struct wl_compositor *compositor;
static struct wl_shm *shm;
static struct zwlr_layer_shell_v1 *layer_shell;
static struct wl_surface *surface;
static struct zwlr_layer_surface_v1 *layer_surface;
static struct wl_display *display;
static int running = 1, W = 640, H = 480, configured = 0, dirty = 1;
struct buf { struct wl_buffer *wlb; uint32_t *px; int w, h, size, busy; };
static struct buf bufs[2];

// ------------------------------------------------------------------ police ---
static stbtt_fontinfo g_font;
static unsigned char *g_font_data;
static int g_have_font = 0;
static void load_font(void){
    FILE *f = fopen(RES_DIR "/SourceCodePro-Black.otf","rb"); if(!f) return;
    fseek(f,0,SEEK_END); long sz=ftell(f); fseek(f,0,SEEK_SET);
    g_font_data=malloc(sz); if(fread(g_font_data,1,sz,f)!=(size_t)sz){fclose(f);return;}
    fclose(f); stbtt_InitFont(&g_font,g_font_data,stbtt_GetFontOffsetForIndex(g_font_data,0));
    g_have_font=1;
}
static inline void blend(uint32_t *d,uint8_t sr,uint8_t sg,uint8_t sb,uint8_t sa){
    if(!sa) return; uint32_t v=*d; uint8_t dr=v>>16,dg=v>>8,db=v,da=v>>24,ia=255-sa;
    *d=((uint32_t)(sa+(uint32_t)da*ia/255)<<24)|((uint32_t)(sr+(uint32_t)dr*ia/255)<<16)
      |((uint32_t)(sg+(uint32_t)dg*ia/255)<<8)|(uint8_t)(sb+(uint32_t)db*ia/255);
}
static void rect(uint32_t*b,int x,int y,int w,int h,uint8_t r,uint8_t g,uint8_t bl,uint8_t a){
    uint8_t pr=(uint32_t)r*a/255,pg=(uint32_t)g*a/255,pb=(uint32_t)bl*a/255;
    for(int yy=y;yy<y+h;yy++){if(yy<0||yy>=H)continue; for(int xx=x;xx<x+w;xx++){if(xx<0||xx>=W)continue; blend(&b[yy*W+xx],pr,pg,pb,a);}}
}
static int text_w(const char*s,int px){
    if(!g_have_font) return 0; float sc=stbtt_ScaleForPixelHeight(&g_font,px); int w=0;
    for(const unsigned char*p=(const unsigned char*)s;*p;p++){int a,l;stbtt_GetCodepointHMetrics(&g_font,*p,&a,&l);w+=(int)(a*sc);}
    return w;
}
static void text(uint32_t*b,const char*s,int x,int y,int px,uint8_t r,uint8_t g,uint8_t bl){
    if(!g_have_font) return; float sc=stbtt_ScaleForPixelHeight(&g_font,px);
    int asc,desc,lg; stbtt_GetFontVMetrics(&g_font,&asc,&desc,&lg); int base=(int)(asc*sc);
    int pen=x;
    for(const unsigned char*p=(const unsigned char*)s;*p;p++){
        int cw,ch,xo,yo; unsigned char*bm=stbtt_GetCodepointBitmap(&g_font,0,sc,*p,&cw,&ch,&xo,&yo);
        int a,l; stbtt_GetCodepointHMetrics(&g_font,*p,&a,&l);
        if(bm){for(int yy=0;yy<ch;yy++){int py=y+base+yo+yy; if(py<0||py>=H)continue;
            for(int xx=0;xx<cw;xx++){int pxx=pen+xo+xx; if(pxx<0||pxx>=W)continue; uint8_t cov=bm[yy*cw+xx]; if(!cov)continue;
            blend(&b[py*W+pxx],(uint32_t)r*cov/255,(uint32_t)g*cov/255,(uint32_t)bl*cov/255,cov);}}
            stbtt_FreeBitmap(bm,NULL);}
        pen+=(int)(a*sc);
    }
}

// ------------------------------------------------------------- config I/O ---
// remplace (ou ajoute) "key = value" dans un fichier ini simple.
static void set_key(const char*path,const char*key,const char*value){
    FILE*f=fopen(path,"r"); char*lines[256]; int n=0; int found=0;
    char buf[512];
    if(f){ while(n<256 && fgets(buf,sizeof buf,f)){
        char*p=buf; while(*p==' '||*p=='\t')p++;
        int match=0; size_t kl=strlen(key);
        if(strncmp(p,key,kl)==0){ const char*q=p+kl; while(*q==' '||*q=='\t')q++; if(*q=='=') match=1; }
        if(match){ char line[512]; snprintf(line,sizeof line,"%s = %s\n",key,value); lines[n++]=strdup(line); found=1; }
        else lines[n++]=strdup(buf);
    } fclose(f); }
    if(!found && n<256){ char line[512]; snprintf(line,sizeof line,"%s = %s\n",key,value); lines[n++]=strdup(line); }
    f=fopen(path,"w"); if(f){ for(int i=0;i<n;i++){fputs(lines[i],f);} fclose(f); }
    for(int i=0;i<n;i++) free(lines[i]);
}
static void get_key(const char*path,const char*key,char*out,int max){
    out[0]=0; FILE*f=fopen(path,"r"); if(!f) return; char buf[512];
    while(fgets(buf,sizeof buf,f)){
        char*p=buf; while(*p==' '||*p=='\t')p++;
        size_t kl=strlen(key);
        if(strncmp(p,key,kl)==0){ const char*q=p+kl; while(*q==' '||*q=='\t')q++;
            if(*q=='='){ q++; while(*q==' '||*q=='\t')q++;
                int i=0; while(*q&&*q!='\n'&&*q!='\r'&&*q!='#'&&i<max-1) out[i++]=*q++;
                while(i>0&&(out[i-1]==' '||out[i-1]=='\t'))i--; out[i]=0; } }
    }
    fclose(f);
}

// ------------------------------------------------------------- le modele ---
enum { F_OSD, F_LED, F_FAN, F_PWR };
typedef struct {
    const char *label;
    const char *disp[6];   // texte affiche (anglais)
    const char *val[6];    // valeur ecrite (NULL si kind special)
    int n, idx, file, kind;
    const char *key;
    const char *label_fr;
    const char *disp_fr[6];
} Item;
// language = en (defaut) | fr, lu dans piboy-osd.conf
static int g_fr = 0;
#define TR(en,fr) (g_fr ? (fr) : (en))
// pour kind==1 (couleur LED) : rouge/vert par preset
static const int led_rgb[][2] = { {0,0},{2,2},{255,0},{0,255},{120,40},{120,120} };

static Item items[] = {
  {"OSD",               {"Off","On"},                          {"0","1"},               2,0,F_OSD,0,"enabled",       "OSD",            {"Off","Actif"}},
  {"  Battery",         {"Off","ES","Game","Everywhere"},      {"off","es","game","both"},4,0,F_OSD,0,"battery_mode","  Batterie",     {"Off","ES","Jeu","Partout"}},
  {"  Temperature",     {"Off","ES","Game","Everywhere"},      {"off","es","game","both"},4,0,F_OSD,0,"temp_mode",   "  Temperature",  {"Off","ES","Jeu","Partout"}},
  {"  CPU",             {"Off","ES","Game","Everywhere"},      {"off","es","game","both"},4,0,F_OSD,0,"cpu_mode",    "  CPU",          {"Off","ES","Jeu","Partout"}},
  {"  Volume",          {"Off","ES","Game","Everywhere"},      {"off","es","game","both"},4,0,F_OSD,0,"volume_mode", "  Volume",       {"Off","ES","Jeu","Partout"}},
  {"  WiFi",            {"Off","ES","Game","Everywhere"},      {"off","es","game","both"},4,0,F_OSD,0,"wifi_mode",   "  WiFi",         {"Off","ES","Jeu","Partout"}},
  {"  Bluetooth",       {"Off","ES","Game","Everywhere"},      {"off","es","game","both"},4,0,F_OSD,0,"bluetooth_mode","  Bluetooth",    {"Off","ES","Jeu","Partout"}},
  {"  Clock",           {"Off","ES","Game","Everywhere"},      {"off","es","game","both"},4,0,F_OSD,0,"clock_mode",  "  Horloge",      {"Off","ES","Jeu","Partout"}},
  {"  Position",        {"Top","Bottom"},                      {"top","bottom"},         2,0,F_OSD,0,"position",     "  Position",     {"Haut","Bas"}},
  {"  Volume display",  {"1s","2s","3s","4s","5s"},            {"1","2","3","4","5"},    5,0,F_OSD,0,"volume_seconds","  Duree volume",{"1s","2s","3s","4s","5s"}},
  {"LED",               {"Static","Battery"},                  {"static","battery"},     2,0,F_LED,0,"mode",         "LED",            {"Fixe","Batterie"}},
  {"  Colour (static)", {"Off","Dim","Red","Green","Amber","White"}, {0,0,0,0,0,0},     6,0,F_LED,K_LEDCOLOR,NULL,  "  Couleur (fixe)",{"Eteinte","Faible","Rouge","Verte","Ambre","Blanche"}},
  {"Fan",               {"Silent","Quiet","Balanced","Cool"},  {"silent","quiet","balanced","cool"},4,0,F_FAN,0,"profile","Ventilateur", {"Silencieux","Doux","Equilibre","Frais"}},
  {"CPU",               {"Eco","Normal","Performance"},        {"powersave","schedutil","performance"},3,0,F_PWR,0,"cpu_governor","CPU",{"Eco","Normal","Performance"}},
  // Options du service piboy (piboy-power.conf). La 1re valeur = defaut (cle absente).
  {"Save game on shutdown", {"On","Off"},                    {"1","0"},               2,0,F_PWR,0,"save_on_shutdown",   "Sauvegarde a l'extinction",{"Actif","Off"}},
  {"Low battery warning", {"10% + 7%","15% + 10%","20% + 10%","Off"}, {"10","15","20","0"},4,0,F_PWR,0,"low_battery_warning","Alerte batterie faible",{"10% + 7%","15% + 10%","20% + 10%","Off"}},
  {"Screen off in standby", {"On","Off"},                    {"1","0"},               2,0,F_PWR,0,"screen_off_standby", "Ecran eteint en veille",   {"Actif","Off"}},
  {"Language",          {"English","Francais"},              {"en","fr"},             2,0,F_OSD,0,"language",           "Langue",                   {"English","Francais"}},
  {"WiFi (radio)",      {"Off","On"},                          {0,0},                    2,0,0,K_WIFI,NULL,          "WiFi (radio)",   {"Off","On"}},
  {"Bluetooth (radio)", {"Off","On"},                          {0,0},                    2,0,0,K_BT,NULL,            "Bluetooth (radio)",{"Off","On"}},
  {"System info",       {"Open >"},                            {0},                      1,0,0,K_INFO,NULL,          "Infos systeme",  {"Ouvrir >"}},
};
static const int NITEMS = sizeof(items)/sizeof(items[0]);
static int sel = 0;
static int info_mode = 0;   // 1 = page "Infos systeme"

static const char *conf_path(int file){ return file==F_OSD?OSD_CONF:file==F_LED?LED_CONF:file==F_FAN?FAN_CONF:PWR_CONF; }

// applique le gouverneur CPU a tous les coeurs, en direct
static void apply_governor(const char*gov){
    for(int c=0;c<8;c++){ char p[96]; snprintf(p,sizeof p,"/sys/devices/system/cpu/cpu%d/cpufreq/scaling_governor",c);
        FILE*f=fopen(p,"w"); if(!f) break; fputs(gov,f); fclose(f); }
}
// 1 si radio active (non bloquee), 0 sinon
static int rfkill_on(const char*dev){
    char cmd[64]; snprintf(cmd,sizeof cmd,"rfkill list %s 2>/dev/null",dev);
    FILE*f=popen(cmd,"r"); if(!f) return 1; char line[256]; int on=1;
    while(fgets(line,sizeof line,f)) if(strstr(line,"Soft blocked:")) on = strstr(line,"yes")?0:1;
    pclose(f); return on;
}
static void popen_line(const char*cmd,char*out,int max){ out[0]=0; FILE*f=popen(cmd,"r"); if(!f)return;
    if(fgets(out,max,f)){char*nl=strchr(out,'\n'); if(nl)*nl=0;} pclose(f); }
static int read_int_f(const char*p){ FILE*f=fopen(p,"r"); if(!f)return -1; int v=-1; if(fscanf(f,"%d",&v)!=1)v=-1; fclose(f); return v; }

static void load_model(void){
    char v[64];
    get_key(OSD_CONF,"language",v,sizeof v); g_fr = !strcmp(v,"fr");
    for(int i=0;i<NITEMS;i++){
        Item*it=&items[i];
        if(it->kind==K_LEDCOLOR){ // couleur LED : deduite de red/green
            char rs[16],gs[16]; get_key(LED_CONF,"red",rs,sizeof rs); get_key(LED_CONF,"green",gs,sizeof gs);
            int r=atoi(rs),g=atoi(gs),best=1;
            for(int k=0;k<6;k++) if(led_rgb[k][0]==r&&led_rgb[k][1]==g){best=k;break;}
            it->idx=best; continue;
        }
        if(it->kind==K_WIFI){ it->idx=rfkill_on("wifi"); continue; }
        if(it->kind==K_BT){ it->idx=rfkill_on("bluetooth"); continue; }
        if(it->kind==K_INFO){ it->idx=0; continue; }
        get_key(conf_path(it->file),it->key,v,sizeof v);
        it->idx = 0;
        for(int k=0;k<it->n;k++) if(it->val[k]&&!strcmp(v,it->val[k])){it->idx=k;break;}
    }
}
static void commit(Item*it){
    if(it->kind==K_LEDCOLOR){ char rs[8],gs[8]; snprintf(rs,8,"%d",led_rgb[it->idx][0]); snprintf(gs,8,"%d",led_rgb[it->idx][1]);
        set_key(LED_CONF,"red",rs); set_key(LED_CONF,"green",gs); return; }
    if(it->kind==K_WIFI){ char c[48]; snprintf(c,sizeof c,"rfkill %s wifi",it->idx?"unblock":"block"); system(c); return; }
    if(it->kind==K_BT){ char c[48]; snprintf(c,sizeof c,"rfkill %s bluetooth",it->idx?"unblock":"block"); system(c); return; }
    if(it->kind==K_INFO){ return; }
    set_key(conf_path(it->file),it->key,it->val[it->idx]);
    if(it->key && !strcmp(it->key,"cpu_governor")) apply_governor(it->val[it->idx]);
    if(it->key && !strcmp(it->key,"language")) g_fr = !strcmp(it->val[it->idx],"fr");
}
static void change(int dir){
    Item*it=&items[sel]; it->idx=(it->idx+dir+it->n)%it->n; commit(it); dirty=1;
}
static void activate(void){
    if(items[sel].kind==K_INFO){ info_mode=1; dirty=1; } else change(+1);
}

// ------------------------------------------------------------------ rendu ---
static void buf_release(void*data,struct wl_buffer*wlb){(void)wlb;((struct buf*)data)->busy=0;}
static const struct wl_buffer_listener buf_listener={.release=buf_release};
static struct buf *get_buf(void){
    for(int i=0;i<2;i++){ struct buf*b=&bufs[i]; if(b->busy) continue;
        if(b->wlb&&(b->w!=W||b->h!=H)){wl_buffer_destroy(b->wlb);munmap(b->px,b->size);b->wlb=NULL;}
        if(!b->wlb){int stride=W*4,size=stride*H,fd=memfd_create("pset",MFD_CLOEXEC);
            if(fd<0||ftruncate(fd,size)<0){if(fd>=0)close(fd);return NULL;}
            b->px=mmap(NULL,size,PROT_READ|PROT_WRITE,MAP_SHARED,fd,0);
            struct wl_shm_pool*pl=wl_shm_create_pool(shm,fd,size);
            b->wlb=wl_shm_pool_create_buffer(pl,0,W,H,stride,WL_SHM_FORMAT_ARGB8888);
            wl_shm_pool_destroy(pl); close(fd);
            wl_buffer_add_listener(b->wlb,&buf_listener,b);
            b->w=W;b->h=H;b->size=size;}
        return b; }
    return NULL;
}
// petit triangle plein (up=1 pointe vers le haut, up=0 vers le bas)
static void tri(uint32_t*px,int cx,int cy,int size,int up,uint8_t r,uint8_t g,uint8_t bl){
    for(int i=0;i<size;i++){ int w = up ? (2*i+1) : (2*(size-1-i)+1);
        rect(px, cx-w/2, cy+i, w, 1, r,g,bl,255); }
}
static void info_row(uint32_t*px,int y,const char*label,const char*value){
    text(px,label,52,y,22,190,200,215);
    text(px,value,300,y,22,255,255,255);
}
static void draw_info(uint32_t*px){
    text(px,TR("SYSTEM INFO","INFOS SYSTEME"),40,28,30,120,200,255);
    rect(px,40,70,W-80,2,80,90,110,255);
    char v[160]; int y=96,rh=34;
    int cap=read_int_f("/sys/class/power_supply/BAT0/capacity");
    int mv=read_int_f("/sys/kernel/xpi_gamecon/battery");
    int ma=read_int_f("/sys/kernel/xpi_gamecon/amps");
    char st[32]; popen_line("cat /sys/class/power_supply/BAT0/status 2>/dev/null",st,sizeof st);
    snprintf(v,sizeof v,"%d%%  %dmV  %dmA  %s",cap,mv,ma,st); info_row(px,y,TR("Battery","Batterie"),v); y+=rh;
    int t=read_int_f("/sys/class/thermal/thermal_zone0/temp"); if(t>1000)t/=1000;
    snprintf(v,sizeof v,"%d C",t); info_row(px,y,"Temperature",v); y+=rh;
    char gov[32]; popen_line("cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null",gov,sizeof gov);
    char la[32]; popen_line("cut -d' ' -f1 /proc/loadavg 2>/dev/null",la,sizeof la);
    snprintf(v,sizeof v,TR("gov %s   load %s","gouv %s   charge %s"),gov,la); info_row(px,y,"CPU",v); y+=rh;
    // busybox hostname n'a pas -I : premiere adresse IPv4 globale
    char ip[80]; popen_line("ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -1",ip,sizeof ip); char*sp=strchr(ip,' '); if(sp)*sp=0;
    info_row(px,y,"IP",ip[0]?ip:"-"); y+=rh;
    char up[32]; popen_line("awk '{h=int($1/3600);m=int($1%3600/60);printf \"%dh%02d\",h,m}' /proc/uptime 2>/dev/null",up,sizeof up);
    info_row(px,y,"Uptime",up); y+=rh;
    // Firmware du microcontroleur : 262 = 0x106 = 1.0.6
    int fw=read_int_f("/sys/kernel/xpi_gamecon/version");
    if(fw>0) snprintf(v,sizeof v,"%x.%x.%x",(fw>>8)&0xF,(fw>>4)&0xF,fw&0xF); else snprintf(v,sizeof v,"-");
    info_row(px,y,TR("MCU firmware","Firmware MCU"),v); y+=rh;
    text(px,TR("B: back","B: retour"),40,H-40,16,160,170,185);
}
static void draw(void){
    struct buf*b=get_buf(); if(!b) return; memset(b->px,0,b->size);
    rect(b->px,0,0,W,H, 12,14,20, 255);                 // fond sombre opaque (ES "loading..." transparaissait)
    if(info_mode){ draw_info(b->px);
        wl_surface_attach(surface,b->wlb,0,0); wl_surface_damage_buffer(surface,0,0,W,H); wl_surface_commit(surface); b->busy=1; return; }
    text(b->px,TR("SYSTEM SETTINGS","REGLAGES SYSTEME"),40,28,30, 120,200,255);
    rect(b->px,40,70,W-80,2, 80,90,110,255);

    int area_top=92, rowh=34, footer=H-40;
    int visible=(footer-18-area_top)/rowh; if(visible<1) visible=1;
    // fenetre de defilement qui suit la selection
    static int top=0;
    if(sel<top) top=sel;
    if(sel>=top+visible) top=sel-visible+1;
    if(top>NITEMS-visible) top=NITEMS-visible;
    if(top<0) top=0;
    int last=top+visible; if(last>NITEMS) last=NITEMS;
    for(int i=top;i<last;i++){
        Item*it=&items[i];
        int ry=area_top+(i-top)*rowh;
        if(i==sel) rect(b->px,32,ry-4,W-64,rowh, 40,90,150,180);
        uint8_t lr=230,lg=235,lb=245;
        text(b->px,TR(it->label,it->label_fr),52,ry,22, lr,lg,lb);
        const char*vd=TR(it->disp[it->idx],it->disp_fr[it->idx]);
        int vw=text_w(vd,22);
        uint8_t vr=140,vg=220,vb=140;
        // valeur "off" grisee
        if(!strcmp(it->disp[it->idx],"Off")) {vr=150;vg=150;vb=150;}
        // fleches autour de la valeur selectionnee
        if(i==sel){ text(b->px,"<",W-64-vw-24,ry,22,255,220,120); text(b->px,">",W-64+8,ry,22,255,220,120); }
        text(b->px,vd,W-64-vw,ry,22, vr,vg,vb);
    }
    // indicateurs "il y a plus au-dessus / en-dessous"
    if(top>0)       tri(b->px, W-22, area_top-2,  7, 1, 190,205,235);
    if(last<NITEMS) tri(b->px, W-22, footer-14,   7, 0, 190,205,235);
    int fy=H-40;
    rect(b->px,40,fy-10,W-80,2, 80,90,110,255);   // separe la liste du pied de page
    text(b->px,TR("Up/Down: move   Left/Right or A: change   B: exit","Haut/Bas: naviguer   G/D ou A: changer   B: quitter"),40,fy,16, 160,170,185);

    wl_surface_attach(surface,b->wlb,0,0); wl_surface_damage_buffer(surface,0,0,W,H); wl_surface_commit(surface); b->busy=1;
}

// ------------------------------------------------------------- layer-shell ---
static void lsc(void*d,struct zwlr_layer_surface_v1*ls,uint32_t serial,uint32_t w,uint32_t h){
    (void)d; if(w>0)W=w; if(h>0)H=h; zwlr_layer_surface_v1_ack_configure(ls,serial); configured=1; dirty=1;
}
static void lsclosed(void*d,struct zwlr_layer_surface_v1*ls){(void)d;(void)ls;running=0;}
static const struct zwlr_layer_surface_v1_listener lsl={.configure=lsc,.closed=lsclosed};
static void rg(void*d,struct wl_registry*r,uint32_t name,const char*i,uint32_t v){(void)d;(void)v;
    if(!strcmp(i,wl_compositor_interface.name))compositor=wl_registry_bind(r,name,&wl_compositor_interface,4);
    else if(!strcmp(i,wl_shm_interface.name))shm=wl_registry_bind(r,name,&wl_shm_interface,1);
    else if(!strcmp(i,zwlr_layer_shell_v1_interface.name))layer_shell=wl_registry_bind(r,name,&zwlr_layer_shell_v1_interface,1);}
static void rgr(void*d,struct wl_registry*r,uint32_t n){(void)d;(void)r;(void)n;}
static const struct wl_registry_listener rl={.global=rg,.global_remove=rgr};

// ------------------------------------------------------------------ evdev ---
static int open_pad(void){
    DIR*d=opendir("/dev/input"); if(!d) return -1; struct dirent*e; int fd=-1;
    char best[64]={0};
    while((e=readdir(d))){
        if(strncmp(e->d_name,"event",5)) continue;
        char path[80]; snprintf(path,sizeof path,"/dev/input/%s",e->d_name);
        int f=open(path,O_RDONLY|O_NONBLOCK); if(f<0) continue;
        char name[128]={0}; if(ioctl(f,EVIOCGNAME(sizeof name),name)>=0 && strstr(name,"PiBoy")){ strcpy(best,path); close(f); break; }
        close(f);
    }
    closedir(d);
    if(best[0]) fd=open(best,O_RDONLY|O_NONBLOCK);
    return fd;
}

int main(void){
    load_font();
    display=wl_display_connect(NULL);
    if(!display){fprintf(stderr,"piboy-settings: pas de Wayland\n");return 1;}
    struct wl_registry*reg=wl_display_get_registry(display);
    wl_registry_add_listener(reg,&rl,NULL); wl_display_roundtrip(display);
    if(!compositor||!shm||!layer_shell){fprintf(stderr,"piboy-settings: layer-shell absent\n");return 1;}

    load_model();

    surface=wl_compositor_create_surface(compositor);
    struct wl_region*empty=wl_compositor_create_region(compositor);
    wl_surface_set_input_region(surface,empty); wl_region_destroy(empty);
    layer_surface=zwlr_layer_shell_v1_get_layer_surface(layer_shell,surface,NULL,ZWLR_LAYER_SHELL_V1_LAYER_OVERLAY,"piboy-settings");
    zwlr_layer_surface_v1_set_anchor(layer_surface,
        ZWLR_LAYER_SURFACE_V1_ANCHOR_TOP|ZWLR_LAYER_SURFACE_V1_ANCHOR_BOTTOM|
        ZWLR_LAYER_SURFACE_V1_ANCHOR_LEFT|ZWLR_LAYER_SURFACE_V1_ANCHOR_RIGHT);
    zwlr_layer_surface_v1_set_exclusive_zone(layer_surface,-1);
    zwlr_layer_surface_v1_set_size(layer_surface,0,0);
    zwlr_layer_surface_v1_add_listener(layer_surface,&lsl,NULL);
    wl_surface_commit(surface); wl_display_roundtrip(display);

    int pad=open_pad();
    if(pad<0) fprintf(stderr,"piboy-settings: manette introuvable (evdev)\n");

    struct pollfd pfd[2]={{.fd=wl_display_get_fd(display),.events=POLLIN},{.fd=pad,.events=POLLIN}};
    while(running){
        if(dirty && configured){ draw(); dirty=0; }
        wl_display_flush(display);
        int np = pad>=0?2:1;
        poll(pfd,np,200);
        if(pfd[0].revents&POLLIN){ if(wl_display_dispatch(display)==-1) break; }
        else wl_display_dispatch_pending(display);
        if(pad>=0 && (pfd[1].revents&POLLIN)){
            struct input_event ev; ssize_t r;
            while((r=read(pad,&ev,sizeof ev))==sizeof ev){
                // En page Infos : seul B/Start revient au menu.
                if(info_mode){
                    if(ev.type==EV_KEY && ev.value==1 && (ev.code==BTN_EAST||ev.code==BTN_START)){ info_mode=0; dirty=1; }
                    continue;
                }
                // D-pad = hat analogique (ABS_HAT0X/Y), PAS des BTN_DPAD_*.
                if(ev.type==EV_ABS){
                    if(ev.code==ABS_HAT0Y){
                        if(ev.value<0){ sel=(sel-1+NITEMS)%NITEMS; dirty=1; }
                        else if(ev.value>0){ sel=(sel+1)%NITEMS; dirty=1; }
                    } else if(ev.code==ABS_HAT0X){
                        if(ev.value<0) change(-1);
                        else if(ev.value>0) activate();
                    }
                    // ABS_X/ABS_Y (stick) volontairement ignores (bruit)
                } else if(ev.type==EV_KEY && ev.value==1){
                    switch(ev.code){
                        case BTN_SOUTH: activate(); break;               // A = valider/changer
                        case BTN_EAST: case BTN_START: running=0; break; // B / Start = quitter
                        default: break;
                    }
                }
            }
        }
    }
    if(pad>=0) close(pad);
    wl_display_disconnect(display);
    return 0;
}
