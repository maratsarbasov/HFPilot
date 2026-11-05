// Real ownership transitions, without a Bluetooth controller or BTstack mocks.
#include <assert.h>
#include "phone.h"
int main(void) {
    assert(phone_number_valid("+12025550123"));
    assert(!phone_number_valid("+12025550123;ATH"));
    assert(!phone_number_valid("+01234567"));
    assert(!phone_number_valid("+1234567890123456"));
    phone_state s={.slc=1};
    assert(phone_reserve_outgoing(&s,2)==-1);
    assert(!phone_reserve_outgoing(&s,1));
    assert(s.generation==1 && s.accepted && s.dial_pending && phone_can_hangup(&s));
    assert(phone_reserve_outgoing(&s,2)==-1); // No duplicate while awaiting indicators.
    phone_indicator(&s,"callsetup",2);
    phone_indicator(&s,"callsetup",3);
    assert(s.generation==1 && s.accepted && !s.dial_pending && phone_can_hangup(&s));
    phone_indicator(&s,"call",1);
    phone_indicator(&s,"callsetup",0);
    assert(s.accepted && phone_can_hangup(&s));
    phone_indicator(&s,"callsetup",1); // Waiting call: CHUP would be ambiguous.
    assert(!phone_can_hangup(&s));
    phone_indicator(&s,"callsetup",0);
    phone_indicator(&s,"callheld",1);
    assert(!phone_can_hangup(&s));
    phone_indicator(&s,"callheld",0);
    phone_indicator(&s,"call",0);
    assert(!s.accepted && !s.outgoing && !phone_can_hangup(&s));
    // Dial-to-idle without ever answering must release ownership.
    assert(!phone_reserve_outgoing(&s,2));
    phone_indicator(&s,"callsetup",2);
    phone_indicator(&s,"callsetup",0);
    assert(!s.accepted && !s.dial_pending && !phone_can_hangup(&s));
    // Incoming wins the race with a pending dial; it gets a different generation.
    assert(!phone_reserve_outgoing(&s,3));
    phone_indicator(&s,"callsetup",1);
    assert(s.generation==4 && !s.accepted && !s.outgoing && !phone_can_hangup(&s));
    assert(phone_reserve_outgoing(&s,5)==-1);
    phone_indicator(&s,"callsetup",0);
    phone_caller_ring(&s);
    assert(phone_reserve_outgoing(&s,5)==-1); // Early RING precedes callsetup.
    phone_caller_clear(&s);
    // Already-active external call never inherits ownership of our old call.
    phone_indicator(&s,"call",1);
    assert(s.generation==5 && !s.accepted && !phone_can_hangup(&s));
    return 0;
}
