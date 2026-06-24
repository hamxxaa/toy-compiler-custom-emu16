// Storage backend for game saves and asset packs.
//
// TODAY this is LittleFS (internal flash). The user runs flash as a stand-in until an SD card
// reader is wired up. To migrate to SD later, this is the ONLY place that should change:
// point STORAGE_FS at the SD filesystem object (e.g. `SD` or an `SDFS`) and make sure it has been
// .begin()'d in setup(). Everything above (save/load + asset syscalls) talks only to these helpers.
#pragma once
#include <Arduino.h>
#include <LittleFS.h>
#include <FS.h>

#define STORAGE_FS LittleFS

namespace storage
{
    // Truncate-and-write `n` bytes to `path`. Returns true iff all bytes were written.
    inline bool write_file(const char *path, const uint8_t *data, size_t n)
    {
        File f = STORAGE_FS.open(path, "w");
        if (!f) return false;
        size_t w = f.write(data, n);
        f.close();
        return w == n;
    }

    // Read up to `max` bytes from `path` into `dst`. Returns bytes read (0 if absent).
    inline size_t read_file(const char *path, uint8_t *dst, size_t max)
    {
        File f = STORAGE_FS.open(path, "r");
        if (!f) return 0;
        size_t n = f.read(dst, max);
        f.close();
        return n;
    }

    inline bool exists(const char *path) { return STORAGE_FS.exists(path); }

    // Open read-only for seek/read streaming (asset packs). Caller closes the returned File.
    inline File open_ro(const char *path) { return STORAGE_FS.open(path, "r"); }

    inline void ensure_dir(const char *path) { STORAGE_FS.mkdir(path); }
}
