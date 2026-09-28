/*
 * lte_sib_decoder: find an LTE cell on a carrier and decode its MIB, SIB1 and SI
 * messages (SIB2..) directly with libsrsran's PHY, without running a whole UE
 * (srsue). The SDR is opened once and retuned per carrier: opening a bladeRF 2.0
 * takes ~7 s, decoding a carrier ~1 s.
 *
 * Output per carrier, in the format parse_save_sib.py reads from srsue:
 *   "Found Cell:  Mode=FDD, PCI=..., PRB=..., Ports=..., CP=..., CFO=... KHz"
 *   "... [I] Content: [{"BCCH-BCH-Message": ...}]"   (MIB, SIB1, SI messages)
 *   "... [I] \t[powermeasure]\t[{"rsrp":-80.1}]"
 *   "... [I] [decoder] done <status>"                 (last line)
 *
 * Modes:
 *   one carrier:  lte_sib_decoder -e 6200 -l /tmp/x.log [options]
 *   persistent:   lte_sib_decoder -s [options], then one line per carrier on stdin:
 *                   "<earfcn> <gain> <freq_offset_hz> <log_file>"
 *                 answered on stdout with "done <earfcn> <status>".
 *   status: sibs (every SIB announced in SIB1), sib1, mib, nocell, error.
 *
 * With -r the SDR stays at a fixed sample rate and lower rates are obtained by
 * FFT decimation (like srsue --rf.srate): sample-rate changes on a bladeRF 2.0
 * take seconds. Without -r the device rate is changed per phase.
 */
#include <algorithm>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <getopt.h>
#include <set>
#include <string>
#include <sys/time.h>

#include "srsran/asn1/rrc.h"
#include "srsran/phy/resampling/resampler.h"
#include "srsran/phy/rf/rf.h"
#include "srsran/srsran.h"
#include "srsran/srslog/srslog.h"

static volatile bool go_exit = false;
static void          on_signal(int) { go_exit = true; }

struct args_t {
  int         earfcn      = -1;
  double      freq_offset = 0;
  float       gain        = 40;
  std::string dev_name    = "";
  std::string dev_args    = "";
  double      hw_srate    = 0;   // 0: change the device rate per phase
  float       search_s    = 5;   // cell search + MIB
  float       sib1_s      = 4;   // SIB1 after the MIB (sent every 80 ms)
  float       si_s        = 6;   // after the last new SIB (SI periodicity up to 5.12 s)
  float       gain_offset = 62;  // srsue's phy.rx_gain_offset, so RSRP values compare
  std::string log_file    = "";
  bool        server      = false;
  bool        verbose     = false;
};

static void usage(const char* prog)
{
  printf("usage: %s -e EARFCN [-l log] [options]   or   %s -s [options]\n"
         "  -e EARFCN   downlink EARFCN (one-carrier mode)\n"
         "  -s          persistent mode: read \"earfcn gain offset_hz logfile\" lines on stdin\n"
         "  -o HZ       frequency offset added when tuning (clock correction, one-carrier mode)\n"
         "  -g DB       RX gain (default 40, one-carrier mode)\n"
         "  -d NAME     RF device (bladeRF, soapy, UHD, ...; default: first found)\n"
         "  -a ARGS     RF device arguments\n"
         "  -r HZ       fixed hardware sample rate, decimated in software (e.g. 30.72e6)\n"
         "  -t S        cell search + MIB time limit (default 5)\n"
         "  -1 S        SIB1 wait after the MIB (default 4)\n"
         "  -T S        wait after the last new SIB (default 6)\n"
         "  -l FILE     log file (default stdout, one-carrier mode)\n"
         "  -v          print each cell search attempt\n",
         prog, prog);
}

static void parse_args(args_t& a, int argc, char** argv)
{
  int opt;
  while ((opt = getopt(argc, argv, "e:so:g:d:a:r:t:1:T:l:vh")) != -1) {
    switch (opt) {
      case 'e': a.earfcn = atoi(optarg); break;
      case 's': a.server = true; break;
      case 'o': a.freq_offset = atof(optarg); break;
      case 'g': a.gain = atof(optarg); break;
      case 'd': a.dev_name = optarg; break;
      case 'a': a.dev_args = optarg; break;
      case 'r': a.hw_srate = atof(optarg); break;
      case 't': a.search_s = atof(optarg); break;
      case '1': a.sib1_s = atof(optarg); break;
      case 'T': a.si_s = atof(optarg); break;
      case 'l': a.log_file = optarg; break;
      case 'v': a.verbose = true; break;
      default: usage(argv[0]); exit(opt == 'h' ? 0 : 1);
    }
  }
  if (!a.server && a.earfcn < 0) {
    usage(argv[0]);
    exit(1);
  }
}

static double now_s()
{
  struct timeval tv;
  gettimeofday(&tv, nullptr);
  return tv.tv_sec + tv.tv_usec * 1e-6;
}

/* ---- log output in srsue's format ---- */
static void log_line(FILE* f, const char* tag, const std::string& text)
{
  struct timeval tv;
  gettimeofday(&tv, nullptr);
  struct tm tm;
  localtime_r(&tv.tv_sec, &tm);
  char ts[32];
  strftime(ts, sizeof(ts), "%Y-%m-%dT%H:%M:%S", &tm);
  fprintf(f, "%s.%06ld [%-7s] [I] %s\n", ts, (long)tv.tv_usec, tag, text.c_str());
  fflush(f);
}

template <class T>
static void log_content(FILE* f, const T& msg)
{
  asn1::json_writer jw;
  msg.to_json(jw);
  std::string s = jw.to_string();
  // same as srsue's rrc::log_rrc_message at info level
  s.erase(std::remove(s.begin(), s.end(), '\n'), s.end());
  s.erase(std::remove(s.begin(), s.end(), '\t'), s.end());
  s.erase(std::remove(s.begin(), s.end(), ' '), s.end());
  log_line(f, "RRC", "Content: " + s);
}

/* ---- RF reception with optional decimation ---- */
struct rx_t {
  srsran_rf_t            rf       = {};
  double                 hw_srate = 0; // fixed hardware rate, 0 if the device rate is changed
  uint32_t               ratio    = 1;
  srsran_resampler_fft_t dec      = {};
  bool                   dec_init = false;
  cf_t*                  tmp      = nullptr;
  uint32_t               tmp_len  = 0;
};

static int set_srate(rx_t& rx, double srate)
{
  if (rx.hw_srate > 0) {
    double   r     = rx.hw_srate / srate;
    uint32_t ratio = (uint32_t)(r + 0.5);
    if (ratio < 1 || fabs(r - ratio) > 1e-6) {
      fprintf(stderr, "hardware rate %.2f MHz is not an integer multiple of %.2f MHz\n", rx.hw_srate / 1e6,
              srate / 1e6);
      return -1;
    }
    if (rx.dec_init && rx.ratio == ratio) {
      srsran_resampler_fft_reset_state(&rx.dec);
      return 0;
    }
    if (rx.dec_init) {
      srsran_resampler_fft_free(&rx.dec);
      rx.dec_init = false;
    }
    if (ratio > 1) {
      if (srsran_resampler_fft_init(&rx.dec, SRSRAN_RESAMPLER_MODE_DECIMATE, ratio)) {
        return -1;
      }
      rx.dec_init = true;
    }
    rx.ratio = ratio;
  } else {
    srsran_rf_stop_rx_stream(&rx.rf);
    double got = srsran_rf_set_rx_srate(&rx.rf, srate);
    if (fabs(got - srate) > 1e3) {
      fprintf(stderr, "could not set sample rate %.2f MHz (got %.2f)\n", srate / 1e6, got / 1e6);
      return -1;
    }
    srsran_rf_start_rx_stream(&rx.rf, false);
  }
  return 0;
}

static int recv_cb(void* h, cf_t* data[SRSRAN_MAX_PORTS], uint32_t nsamples, srsran_timestamp_t* t)
{
  rx_t&    rx  = *(rx_t*)h;
  uint32_t n   = nsamples * rx.ratio;
  cf_t*    dst = data[0];
  if (rx.ratio > 1) {
    if (n > rx.tmp_len) {
      free(rx.tmp);
      rx.tmp     = srsran_vec_cf_malloc(n);
      rx.tmp_len = n;
    }
    dst = rx.tmp;
  }
  void* ptr[SRSRAN_MAX_CHANNELS] = {};
  ptr[0]                         = dst;
  int ret = srsran_rf_recv_with_time_multi(&rx.rf, ptr, n, true, t ? &t->full_secs : nullptr,
                                           t ? &t->frac_secs : nullptr);
  if (ret < 0 || rx.ratio == 1) {
    return ret;
  }
  srsran_resampler_fft_run(&rx.dec, rx.tmp, data[0], n);
  return nsamples;
}

/* ---- SI bookkeeping ---- */
struct si_state_t {
  bool          have_sib1 = false;
  std::set<int> expected; // SIB numbers announced in SIB1 (plus SIB2)
  std::set<int> received;
  bool          done() const
  {
    return have_sib1 && std::includes(received.begin(), received.end(), expected.begin(), expected.end());
  }
};

static int sib_number(const std::string& s)
{
  // "sib2", "sib13-v920", "sibType3" -> 2, 13, 3
  size_t i = s.find_first_of("0123456789");
  return i == std::string::npos ? -1 : atoi(s.c_str() + i);
}

// returns true if something new was decoded
static bool handle_dlsch(FILE* f, si_state_t& st, uint8_t* payload, uint32_t nbytes)
{
  asn1::rrc::bcch_dl_sch_msg_s msg;
  asn1::cbit_ref               bref(payload, nbytes);
  if (msg.unpack(bref) != asn1::SRSASN_SUCCESS ||
      msg.msg.type().value != asn1::rrc::bcch_dl_sch_msg_type_c::types_opts::c1) {
    return false;
  }
  auto& c1 = msg.msg.c1();
  if (c1.type().value == asn1::rrc::bcch_dl_sch_msg_type_c::c1_c_::types_opts::sib_type1) {
    if (st.have_sib1) {
      return false;
    }
    st.have_sib1 = true;
    st.expected.insert(2);
    for (auto& si : c1.sib_type1().sched_info_list) {
      for (auto& t : si.sib_map_info) {
        st.expected.insert(sib_number(t.to_string()));
      }
    }
    log_content(f, msg);
    return true;
  }
  if (c1.type().value == asn1::rrc::bcch_dl_sch_msg_type_c::c1_c_::types_opts::sys_info) {
    auto& si = c1.sys_info();
    if (si.crit_exts.type().value != asn1::rrc::sys_info_s::crit_exts_c_::types_opts::sys_info_r8) {
      return false;
    }
    bool is_new = false;
    for (auto& item : si.crit_exts.sys_info_r8().sib_type_and_info) {
      int n = sib_number(item.type().to_string());
      if (n > 0 && !st.received.count(n)) {
        st.received.insert(n);
        is_new = true;
      }
    }
    if (is_new) {
      log_content(f, msg);
    }
    return is_new;
  }
  return false;
}

/* ---- PDSCH with SI-RNTI ----
 * Like srsran_ue_dl_find_and_decode, but when the DCI carries no redundancy
 * version (format 1C) ue_dl applies SIB1's RV formula to every SI message. The
 * RV of an SI message depends on its position in the SI window (36.321 5.3.1),
 * so here the four RVs are tried until the CRC passes.
 * Returns -1 on error, 0 without an SI-RNTI DCI, 1 with a DCI (*crc = decoded). */
static int decode_si(srsran_ue_dl_t* q, srsran_dl_sf_cfg_t* sf, srsran_ue_dl_cfg_t* cfg, srsran_pdsch_cfg_t* pdsch,
                     uint8_t* payload, bool search, bool is_sib1, bool* crc)
{
  *crc = false;
  srsran_ue_dl_set_mi_auto(q);
  if (srsran_ue_dl_decode_fft_estimate(q, sf, cfg) < 0) {
    return -1;
  }
  if (!search) {
    return 0; // channel estimate only (RSRP and CFO tracking)
  }
  srsran_dci_dl_t dci[SRSRAN_MAX_DCI_MSG] = {};
  if (srsran_ue_dl_find_dl_dci(q, sf, cfg, SRSRAN_SIRNTI, dci) != 1) {
    return 0;
  }
  if (srsran_ue_dl_dci_to_pdsch_grant(q, sf, cfg, &dci[0], &pdsch->grant) || !pdsch->grant.tb[0].enabled) {
    return 0;
  }
  int      rvs[4] = {0, 2, 3, 1};
  uint32_t nrv    = 4;
  if (pdsch->grant.tb[0].rv >= 0) {
    rvs[0] = pdsch->grant.tb[0].rv;
    nrv    = 1;
  } else if (is_sib1) {
    uint32_t k = (sf->tti / 10 / 2) % 4;
    rvs[0]     = ((uint32_t)ceilf(1.5f * k)) % 4;
    nrv        = 1;
  }
  for (uint32_t i = 0; i < nrv && !*crc; i++) {
    pdsch->grant.tb[0].rv = rvs[i];
    srsran_softbuffer_rx_reset_tbs(pdsch->softbuffers.rx[0], (uint32_t)pdsch->grant.tb[0].tbs);
    srsran_pdsch_res_t res[SRSRAN_MAX_CODEWORDS] = {};
    res[0].payload                               = payload;
    if (srsran_ue_dl_decode_pdsch(q, sf, pdsch, res)) {
      return -1;
    }
    *crc = res[0].crc;
  }
  return 1;
}

/* ---- one carrier: search, MIB, SIB1, SI messages ---- */
static const char* process_carrier(rx_t& rx, const args_t& args, int earfcn, float gain, double offset, FILE* f)
{
  double t0 = now_s();
  double dl = srsran_band_fd(earfcn) * 1e6;
  if (dl <= 0) {
    return "error";
  }
  srsran_rf_set_rx_gain(&rx.rf, gain);
  srsran_rf_set_rx_freq(&rx.rf, 0, dl + offset);
  printf("EARFCN %d: %.1f MHz (offset %.0f Hz), gain %.0f\n", earfcn, dl / 1e6, offset, gain);

  /* 1. PSS/SSS search and MIB at 1.92 MSPS */
  double        t_search = t0 + args.search_s;
  float         cfo      = 0;
  srsran_cell_t cell     = {};
  uint8_t       bch[SRSRAN_BCH_PAYLOAD_LEN] = {};
  {
    // PSS/SSS candidates in PSR order; a candidate only counts once its PBCH
    // decodes (a weak or false PSS/SSS detection fails there), else search again
    srsran_ue_cellsearch_t cs;
    srsran_ue_mib_sync_t   ue_mib;
    if (srsran_ue_cellsearch_init_multi(&cs, SRSRAN_DEFAULT_MAX_FRAMES_PSS, recv_cb, 1, &rx)) {
      return "error";
    }
    if (srsran_ue_mib_sync_init_multi(&ue_mib, recv_cb, 1, &rx)) {
      srsran_ue_cellsearch_free(&cs);
      return "error";
    }
    srsran_ue_cellsearch_set_nof_valid_frames(&cs, SRSRAN_DEFAULT_NOF_VALID_PSS_FRAMES);
    bool found = false;
    while (!found && !go_exit && now_s() < t_search) {
      srsran_ue_cellsearch_result_t res[3] = {};
      uint32_t                      best   = 0;
      if (set_srate(rx, SRSRAN_CS_SAMP_FREQ) || srsran_ue_cellsearch_scan(&cs, res, &best) < 0) {
        break;
      }
      int order[3] = {0, 1, 2};
      std::sort(order, order + 3, [&](int a, int b) { return res[a].psr > res[b].psr; });
      for (int k = 0; k < 3 && !found && !go_exit && now_s() < t_search; k++) {
        const srsran_ue_cellsearch_result_t& r = res[order[k]];
        if (r.psr <= 0 || r.mode <= 0) {
          continue;
        }
        cell            = {};
        cell.id         = r.cell_id;
        cell.cp         = r.cp;
        cell.frame_type = r.frame_type;
        srsran_ue_mib_sync_set_cell(&ue_mib, cell);
        srsran_ue_sync_reset(&ue_mib.ue_sync);
        ue_mib.ue_sync.cfo_current_value       = r.cfo / 15000;
        ue_mib.ue_sync.cfo_is_copied           = true;
        ue_mib.ue_sync.cfo_correct_enable_find = true;
        srsran_sync_set_cfo_cp_enable(&ue_mib.ue_sync.sfind, false, 0);
        int n = srsran_ue_mib_sync_decode(&ue_mib, 40, bch, &cell.nof_ports, nullptr);
        if (args.verbose) {
          printf("  candidate PCI %d PSR %.2f mode %.2f CFO %.0f Hz: MIB %s (%.1f s)\n", r.cell_id, r.psr, r.mode,
                 r.cfo, n == 1 ? "yes" : "no", now_s() - t0);
        }
        if (n == 1) {
          cfo   = srsran_ue_sync_get_cfo(&ue_mib.ue_sync);
          found = true;
        }
      }
    }
    srsran_ue_mib_sync_free(&ue_mib);
    srsran_ue_cellsearch_free(&cs);
    if (!found) {
      return "nocell";
    }
  }

  /* 2. MIB */
  {
    srsran_pbch_mib_unpack(bch, &cell, nullptr);
    char line[160];
    snprintf(line, sizeof(line), "Found Cell:  Mode=%s, PCI=%d, PRB=%d, Ports=%d, CP=%s, CFO=%.1f KHz",
             cell.frame_type == SRSRAN_FDD ? "FDD" : "TDD", cell.id, cell.nof_prb, cell.nof_ports,
             cell.cp == SRSRAN_CP_NORM ? "Normal" : "Extended", cfo / 1e3);
    printf("%s (%.1f s)\n", line, now_s() - t0);
    log_line(f, "PHY", line);
    uint8_t                   packed[4] = {};
    asn1::rrc::bcch_bch_msg_s mib;
    srsran_bit_pack_vector(bch, packed, SRSRAN_BCH_PAYLOAD_LEN);
    asn1::cbit_ref bref(packed, 3);
    if (mib.unpack(bref) == asn1::SRSASN_SUCCESS) {
      log_content(f, mib);
    }
  }

  /* 3. SIB1 and SI messages at the cell's sample rate */
  int srate = srsran_sampling_freq_hz(cell.nof_prb);
  if (srate < 0 || set_srate(rx, srate)) {
    fprintf(stderr, "EARFCN %d: %d PRB not supported at this hardware rate\n", earfcn, cell.nof_prb);
    return "mib";
  }
  uint32_t max_samples                 = 3 * SRSRAN_SF_LEN_PRB(cell.nof_prb);
  cf_t*    sf_buf[SRSRAN_MAX_PORTS]    = {};
  uint8_t* data[SRSRAN_MAX_CODEWORDS] = {};
  sf_buf[0]                            = srsran_vec_cf_malloc(max_samples);
  data[0]                              = srsran_vec_u8_malloc(2000 * 8);

  srsran_ue_sync_t       ue_sync;
  srsran_ue_mib_t        ue_mib;
  srsran_ue_dl_t         ue_dl;
  srsran_ue_dl_cfg_t     dl_cfg    = {};
  srsran_dl_sf_cfg_t     sf_cfg    = {};
  srsran_pdsch_cfg_t     pdsch_cfg = {};
  srsran_softbuffer_rx_t softbuf;
  const char*            status = "mib";
  if (srsran_ue_sync_init_multi_decim(&ue_sync, cell.nof_prb, false, recv_cb, 1, &rx, 0) ||
      srsran_ue_sync_set_cell(&ue_sync, cell) || srsran_ue_mib_init(&ue_mib, sf_buf[0], cell.nof_prb) ||
      srsran_ue_mib_set_cell(&ue_mib, cell) || srsran_ue_dl_init(&ue_dl, sf_buf, cell.nof_prb, 1) ||
      srsran_ue_dl_set_cell(&ue_dl, cell) || srsran_softbuffer_rx_init(&softbuf, cell.nof_prb)) {
    fprintf(stderr, "Error initialising the PHY\n");
    return "error";
  }
  ue_sync.cfo_current_value        = cfo / 15000;
  ue_sync.cfo_is_copied            = true;
  ue_sync.cfo_correct_enable_find  = true;
  ue_sync.cfo_correct_enable_track = true;
  srsran_sync_set_cfo_cp_enable(&ue_sync.sfind, false, 0);

  // receiver settings as srsue's defaults (phy_common::set_ue_dl_cfg / set_pdsch_cfg)
  dl_cfg.chest_cfg.filter_type          = SRSRAN_CHEST_FILTER_GAUSS;
  dl_cfg.chest_cfg.filter_coef[0]       = 4;
  dl_cfg.chest_cfg.filter_coef[1]       = 1.0f;
  dl_cfg.chest_cfg.noise_alg            = SRSRAN_NOISE_ALG_REFS;
  dl_cfg.chest_cfg.estimator_alg        = SRSRAN_ESTIMATOR_ALG_AVERAGE;
  dl_cfg.chest_cfg.cfo_estimate_enable  = true;
  dl_cfg.chest_cfg.cfo_estimate_sf_mask = 1023;
  dl_cfg.cfg.tm                         = cell.nof_ports > 1 ? SRSRAN_TM2 : SRSRAN_TM1;
  pdsch_cfg.softbuffers.rx[0]           = &softbuf;
  pdsch_cfg.rnti                        = SRSRAN_SIRNTI;
  pdsch_cfg.max_nof_iterations          = 8;
  pdsch_cfg.decoder_type                = SRSRAN_MIMO_DECODER_MMSE;
  pdsch_cfg.csi_enable                  = true;

  si_state_t st;
  bool       have_sfn  = false;
  uint32_t   sfn       = 0;
  float      rsrp_sum  = 0;
  int        rsrp_n    = 0;
  bool       rsrp_done = false;
  double     deadline  = now_s() + args.sib1_s;
  uint32_t   n_sf = 0, n_nosync = 0, n_nosfn = 0, n_dci = 0, n_crc = 0;
  uint32_t   prev_sf   = 0;
  uint32_t   frames_since_pbch = 0;
  double     last_sync = now_s();
  double     last_sfn  = now_s();
  uint32_t   n_reset   = 0;
  while (!go_exit && now_s() < deadline && !(st.done() && rsrp_done)) {
    cf_t* bufs[SRSRAN_MAX_CHANNELS] = {sf_buf[0]};
    int   n                         = srsran_ue_sync_zerocopy(&ue_sync, bufs, max_samples);
    if (n < 0) {
      fprintf(stderr, "ue_sync error\n");
      break;
    }
    if (n != 1) {
      have_sfn = false; // lost sync: read the SFN again from the PBCH
      n_nosync++;
      if (now_s() - last_sync > 1.0) {
        break; // lost the cell
      }
      continue;
    }
    last_sync = now_s();
    n_sf++;
    uint32_t sf_idx = srsran_ue_sync_get_sfidx(&ue_sync);
    if (have_sfn && sf_idx != (prev_sf + 1) % 10) {
      have_sfn = false; // subframes lost (samples dropped): the SFN count is off
    }
    prev_sf = sf_idx;
    // read the SFN from the PBCH when unknown, and check it every 32 frames
    if (sf_idx == 0 && (!have_sfn || ++frames_since_pbch >= 32)) {
      uint8_t bch[SRSRAN_BCH_PAYLOAD_LEN];
      int     sfn_offset = 0;
      if (srsran_ue_mib_decode(&ue_mib, bch, nullptr, &sfn_offset) == SRSRAN_UE_MIB_FOUND) {
        srsran_cell_t c       = cell;
        uint32_t      new_sfn = 0;
        srsran_pbch_mib_unpack(bch, &c, &new_sfn);
        sfn               = (new_sfn + sfn_offset) % 1024;
        have_sfn          = true;
        frames_since_pbch = 0;
      }
    }
    if (!have_sfn) {
      n_nosfn++;
      // synced but no PBCH at "subframe 0": subframe 0/5 taken from a wrong SSS
      // decision stays wrong while tracking; search the timing again
      if (now_s() - last_sfn > 0.2) {
        srsran_ue_sync_reset(&ue_sync);
        last_sfn = now_s();
        n_reset++;
      }
      continue;
    }
    last_sfn = now_s();
    // SIB1: subframe 5 of even frames; SI messages: in the other subframes
    bool sib1_sf = sf_idx == 5 && sfn % 2 == 0;
    bool search  = st.have_sib1 ? !sib1_sf : sib1_sf;
    sf_cfg.tti   = sfn * 10 + sf_idx;
    bool crc     = false;
    int  nb      = decode_si(&ue_dl, &sf_cfg, &dl_cfg, &pdsch_cfg, data[0], search, sib1_sf, &crc);
    if (nb < 0) {
      fprintf(stderr, "PDSCH decoding error\n");
      break;
    }
    // CFO measured on the reference signals feeds ue_sync's tracking loop, as in srsue
    if (std::isnormal(ue_dl.chest_res.cfo)) {
      srsran_ue_sync_set_cfo_ref(&ue_sync, ue_dl.chest_res.cfo);
    }
    if (search) {
      if (!rsrp_done && isnormal(ue_dl.chest_res.rsrp_dbm)) {
        rsrp_sum += ue_dl.chest_res.rsrp_dbm;
        if (++rsrp_n == 20) {
          char buf[64];
          snprintf(buf, sizeof(buf), "\t[powermeasure]\t[{\"rsrp\":%.1f}]",
                   rsrp_sum / rsrp_n - (gain + args.gain_offset));
          log_line(f, "PHY", buf);
          rsrp_done = true;
        }
      }
      n_dci += nb > 0;
      n_crc += crc;
      if (args.verbose && (!st.have_sib1 || nb > 0)) {
        printf("  sfn %4d sf %d: dci %d crc %s tbs %d snr %.1f dB\n", sfn, sf_idx, nb, crc ? "ok" : "--",
               pdsch_cfg.grant.tb[0].tbs, ue_dl.chest_res.snr_db);
      }
      if (crc && handle_dlsch(f, st, data[0], pdsch_cfg.grant.tb[0].tbs / 8)) {
        deadline = now_s() + args.si_s; // something new: keep listening
      }
    }
    if (sf_idx == 9) {
      sfn = (sfn + 1) % 1024;
    }
  }
  status = st.done() ? "sibs" : st.have_sib1 ? "sib1" : "mib";
  printf("EARFCN %d: %s, %zu of %zu SI SIBs (%.1f s)\n", earfcn, status, st.received.size(), st.expected.size(),
         now_s() - t0);
  if (args.verbose) {
    printf("  subframes %u, not synced %u, no SFN %u (resets %u), SI-RNTI DCIs %u, CRC ok %u\n", n_sf, n_nosync,
           n_nosfn, n_reset, n_dci, n_crc);
  }

  srsran_ue_dl_free(&ue_dl);
  srsran_ue_mib_free(&ue_mib);
  srsran_ue_sync_free(&ue_sync);
  srsran_softbuffer_rx_free(&softbuf);
  free(sf_buf[0]);
  free(data[0]);
  return status;
}

static const char* run_carrier(rx_t& rx, const args_t& args, int earfcn, float gain, double offset, const char* path)
{
  FILE* f = stdout;
  if (path && *path) {
    f = fopen(path, "w");
    if (!f) {
      perror(path);
      return "error";
    }
  }
  const char* status = process_carrier(rx, args, earfcn, gain, offset, f);
  if (strcmp(status, "mib") == 0 && !go_exit) {
    status = process_carrier(rx, args, earfcn, gain, offset, f); // cell there, no SIB1: once more
  }
  log_line(f, "DEC", std::string("[decoder] done ") + status);
  if (f != stdout) {
    fclose(f);
  }
  return status;
}

int main(int argc, char** argv)
{
  args_t args;
  parse_args(args, argc, argv);
  setvbuf(stdout, nullptr, _IOLBF, 0); // line by line, also into a pipe
  srslog::init();
  signal(SIGINT, on_signal);
  signal(SIGTERM, on_signal);
  srsran_use_standard_symbol_size(true);

  rx_t rx;
  rx.hw_srate = args.hw_srate;
  double t0   = now_s();
  if (srsran_rf_open_devname(&rx.rf, args.dev_name.empty() ? nullptr : args.dev_name.c_str(),
                             (char*)args.dev_args.c_str(), 1)) {
    fprintf(stderr, "Error opening RF device\n");
    printf("error opening the RF device\n");
    return 1;
  }
  if (rx.hw_srate > 0) {
    srsran_rf_set_rx_srate(&rx.rf, rx.hw_srate);
    srsran_rf_start_rx_stream(&rx.rf, false);
  }
  printf("ready (RF device opened in %.1f s)\n", now_s() - t0);

  int ret = 0;
  if (args.server) {
    char line[1024];
    while (!go_exit && fgets(line, sizeof(line), stdin)) {
      int    earfcn = -1;
      float  gain   = args.gain;
      double offset = 0;
      char   path[900] = "";
      if (sscanf(line, "%d %f %lf %899s", &earfcn, &gain, &offset, path) < 1 || earfcn < 0) {
        continue;
      }
      const char* status = run_carrier(rx, args, earfcn, gain, offset, path);
      printf("done %d %s\n", earfcn, status);
    }
  } else {
    const char* status = run_carrier(rx, args, args.earfcn, args.gain, args.freq_offset, args.log_file.c_str());
    ret                = strcmp(status, "sibs") == 0 || strcmp(status, "sib1") == 0 ? 0 : 2;
  }

  srsran_rf_stop_rx_stream(&rx.rf);
  srsran_rf_close(&rx.rf);
  return ret;
}
