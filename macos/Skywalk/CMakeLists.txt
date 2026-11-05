cmake_minimum_required(VERSION 3.18)
project(HFPilotSkywalk C)
set(CMAKE_C_STANDARD 11)
get_filename_component(DEFAULT_BTSTACK_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/../../.work/deps/btstack" ABSOLUTE)
set(BTSTACK_ROOT "${DEFAULT_BTSTACK_ROOT}" CACHE PATH "Pinned BTstack checkout")
if(NOT EXISTS "${BTSTACK_ROOT}/src/hci.c")
  message(FATAL_ERROR "BTstack is missing. Run python scripts/prepare_native.py first.")
endif()
include_directories(. ${BTSTACK_ROOT}/src ${BTSTACK_ROOT}/platform/posix)
include_directories(${BTSTACK_ROOT}/3rd-party/bluedroid/encoder/include
                    ${BTSTACK_ROOT}/3rd-party/bluedroid/decoder/include)
file(GLOB SBC_SOURCES CONFIGURE_DEPENDS
  "${BTSTACK_ROOT}/3rd-party/bluedroid/encoder/srce/*.c"
  "${BTSTACK_ROOT}/3rd-party/bluedroid/decoder/srce/*.c")
add_library(skywalk_sbc STATIC ${SBC_SOURCES}
  ${BTSTACK_ROOT}/src/classic/btstack_sbc_bluedroid.c
  ${BTSTACK_ROOT}/src/classic/btstack_sbc_plc.c)
set(CORE_SOURCES)
foreach(NAME ad_parser btstack_memory btstack_memory_pool btstack_linked_list btstack_util
             btstack_run_loop btstack_tlv btstack_crypto btstack_ring_buffer
             hci_cmd hci_dump l2cap l2cap_signaling)
  list(APPEND CORE_SOURCES "${BTSTACK_ROOT}/src/${NAME}.c")
endforeach()
# The supported Skywalk SCO path does not deliver completed-packet events.
# Disable explicit SCO credits and use BTstack's existing receive-paced mode.
# Keep the pinned upstream checkout unchanged; patch only the build-local copy.
file(READ "${BTSTACK_ROOT}/src/hci.c" HCI_SOURCE)
foreach(OLD "hci_send_cmd(&hci_write_synchronous_flow_control_enable, 1)" "hci_stack->synchronous_flow_control_enabled = 1;")
  string(FIND "${HCI_SOURCE}" "${OLD}" MATCH_OFFSET)
  if(MATCH_OFFSET EQUAL -1)
    message(FATAL_ERROR "Pinned BTstack flow-control patch no longer matches")
  endif()
endforeach()
string(REPLACE "hci_send_cmd(&hci_write_synchronous_flow_control_enable, 1)" "hci_send_cmd(&hci_write_synchronous_flow_control_enable, 0)" HCI_SOURCE "${HCI_SOURCE}")
string(REPLACE "hci_stack->synchronous_flow_control_enabled = 1;" "hci_stack->synchronous_flow_control_enabled = 0;" HCI_SOURCE "${HCI_SOURCE}")
file(WRITE "${CMAKE_CURRENT_BINARY_DIR}/hci_skywalk.c" "${HCI_SOURCE}")
list(APPEND CORE_SOURCES "${CMAKE_CURRENT_BINARY_DIR}/hci_skywalk.c")
add_library(btstack_core STATIC ${CORE_SOURCES}
  ${BTSTACK_ROOT}/src/classic/rfcomm.c
  ${BTSTACK_ROOT}/src/classic/sdp_server.c
  ${BTSTACK_ROOT}/src/classic/sdp_client.c
  ${BTSTACK_ROOT}/src/classic/sdp_client_rfcomm.c
  ${BTSTACK_ROOT}/src/classic/sdp_util.c
  ${BTSTACK_ROOT}/src/classic/hfp.c
  ${BTSTACK_ROOT}/src/classic/hfp_hf.c
  ${BTSTACK_ROOT}/src/classic/btstack_link_key_db_memory.c
  ${BTSTACK_ROOT}/src/classic/btstack_link_key_db_tlv.c
  ${BTSTACK_ROOT}/platform/posix/btstack_tlv_posix.c
  ${BTSTACK_ROOT}/platform/posix/btstack_run_loop_posix.c)
target_compile_options(btstack_core PRIVATE -Wall -Wextra)
add_executable(skywalk-hfp main.c transport.c phone.c sco_codec.c call_state.c caller_id.c agent_ipc.c pairing.c)
target_compile_options(skywalk-hfp PRIVATE -Wall -Wextra)
target_link_libraries(skywalk-hfp PRIVATE skywalk_sbc btstack_core "-framework CoreFoundation" "-framework IOKit")
# Built only on request by the offline codec tests; it cannot open Bluetooth.
add_executable(skywalk-codec-test EXCLUDE_FROM_ALL ../../tests/fixtures/skywalk_codec.c sco_codec.c)
target_compile_options(skywalk-codec-test PRIVATE -Wall -Wextra -UNDEBUG)
target_link_libraries(skywalk-codec-test PRIVATE skywalk_sbc btstack_core)
add_executable(skywalk-pairing-test EXCLUDE_FROM_ALL ../../tests/fixtures/skywalk_pairing.c)
target_compile_options(skywalk-pairing-test PRIVATE -Wall -Wextra -UNDEBUG)
target_link_libraries(skywalk-pairing-test PRIVATE btstack_core)
