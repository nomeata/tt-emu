#include "api.h"
#include "sdk.c"
#include "gme_media.h"

/*
 * Minimal embedded main-binary for the tt-emu load-path test.
 *
 * The firmware (state 69 -> 67 -> gme_launch_binary_build_sysapi @0x080aa934)
 * loads this blob to 0x08132000 and calls it with r0 = &system_api. We leave a
 * word trail at a fixed, in-region address (0x08141F00, inside the 64 KiB the
 * loader reserves at 0x08132000) so a host-side test can prove, purely by
 * reading RAM, that:
 *   MARK[0] the embedded code ran at the load address,
 *   MARK[1] a system_api call returned a real firmware value (is_audio_playing),
 *   MARK[2] control returned from that call,
 *   MARK[3] control returned from play_sound (the audio-producing system_api).
 */

#define MARK ((volatile unsigned int *)0x08141F00u)

void main(system_api *apiOrgi) {
    initTT(apiOrgi);
    MARK[0] = 0xDEADBEEFu;                                   /* entry executed  */
    MARK[1] = (unsigned int)api->is_audio_playing();         /* system_api call */
    MARK[2] = 0x5A5A0001u;                                   /* call returned   */
    playSoundNow(MEDIA_START);                               /* play_sound      */
    MARK[3] = 0x5A5A0002u;                                   /* play returned   */
}
