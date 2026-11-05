#ifndef SKYWALK_PAIRING_H
#define SKYWALK_PAIRING_H
#include <stdint.h>
int pairing_init(int fd);
void pairing_working(void);
void pairing_event(uint8_t type, uint8_t *packet, uint16_t size);
void pairing_tick(void);
void pairing_stop(void);
int pairing_finished(void);
#endif
