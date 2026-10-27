/* Independent check: parse the spike's iPod with libgpod (the reference C implementation). */
#include <gpod/itdb.h>
#include <stdio.h>

int main(int argc, char **argv) {
    GError *err = NULL;
    Itdb_iTunesDB *db = itdb_parse(argv[1], &err);
    if (!db) { fprintf(stderr, "itdb_parse failed: %s\n", err ? err->message : "?"); return 1; }
    const Itdb_IpodInfo *info = itdb_device_get_ipod_info(db->device);
    printf("libgpod model: %s %s\n",
           itdb_info_get_ipod_model_name_string(info->ipod_model),
           itdb_info_get_ipod_generation_string(info->ipod_generation));
    printf("tracks: %u, playlists: %u\n", itdb_tracks_number(db), itdb_playlists_number(db));
    for (GList *l = db->tracks; l; l = l->next) {
        Itdb_Track *t = l->data;
        gchar *path = itdb_filename_on_ipod(t);
        printf("  %-12s | %-15s | %-11s | trk %d | comp %d | %s | thumbs %d | plays %u | file %s\n",
               t->title, t->albumartist ? t->albumartist : "-", t->album, t->track_nr,
               t->compilation, t->filetype, itdb_track_has_thumbnails(t), t->playcount,
               path ? "ok" : "MISSING");
        g_free(path);
    }
    for (GList *l = db->playlists; l; l = l->next) {
        Itdb_Playlist *pl = l->data;
        printf("  playlist '%s' master=%d items=%d:", pl->name, itdb_playlist_is_mpl(pl), pl->num);
        if (!itdb_playlist_is_mpl(pl))
            for (GList *m = pl->members; m; m = m->next) printf(" [%s]", ((Itdb_Track *)m->data)->title);
        printf("\n");
    }
    itdb_free(db);
    return 0;
}
