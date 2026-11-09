/* SPDX-License-Identifier: LGPL-2.1-or-later */
/*
 * Drive libcamera-gts9u's SoftAf (packaging/libcamera/patches/0009-*.patch)
 * against a simulated lens and scene, the way the soft IPA does: one call
 * per frame, contrast only on the frames that carry statistics.
 *
 *   soft_af_sim KEY=VALUE...
 *
 * Scene: segments "frame:peak:base:amp:sigma[,...]", each from its frame on;
 * contrast = base * (1 + amp * exp(-(lens - peak)^2 / (2 sigma^2))), times
 * (1 + noise * N(0, 1)).  shake=FROM:TO:FACTOR multiplies contrast over a
 * frame range (blur from moving the tablet).  pan=FROM:TO:SPREAD replaces it
 * with base * U(1 - SPREAD, 1 + SPREAD) (a moving scene).
 *
 * The lens applies a command latency= frames after it was given (the IPA
 * assumes 3).  Statistics come every 4th frame, as with kStatPerNumFrames.
 * mode=continuous|auto, trigger=FRAME (AfTriggerStart), start=POSITION (the
 * lens before the first frame), restart=FRAME (a new stream: reset(),
 * frame numbers from 0).
 *
 * Prints one CSV row per frame: frame,valid,score,command,lens,state.
 */

#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <map>
#include <optional>
#include <random>
#include <sstream>
#include <string>
#include <vector>

#include "soft_af.h"

using libcamera::ipa::soft::SoftAf;

namespace {

struct Segment {
	int frame;
	double peak, base, amp, sigma;
};

std::vector<double> split(const std::string &s, char sep)
{
	std::vector<double> out;
	std::stringstream ss(s);
	std::string item;
	while (std::getline(ss, item, sep))
		out.push_back(std::stod(item));
	return out;
}

} /* namespace */

int main(int argc, char **argv)
{
	std::map<std::string, std::string> args{
		{ "frames", "900" }, { "noise", "0.03" }, { "seed", "1" },
		{ "latency", "2" }, { "mode", "continuous" }, { "trigger", "-1" },
		{ "start", "512" }, { "restart", "-1" }, { "shake", "" },
		{ "pan", "" }, { "scene", "0:400:1000:1.0:70" },
	};
	for (int i = 1; i < argc; ++i) {
		const char *eq = std::strchr(argv[i], '=');
		if (!eq) {
			std::fprintf(stderr, "bad argument %s\n", argv[i]);
			return 2;
		}
		args[std::string(argv[i], eq - argv[i])] = eq + 1;
	}

	std::vector<Segment> scene;
	{
		std::stringstream ss(args["scene"]);
		std::string seg;
		while (std::getline(ss, seg, ',')) {
			std::vector<double> v = split(seg, ':');
			if (v.size() != 5) {
				std::fprintf(stderr, "bad segment %s\n", seg.c_str());
				return 2;
			}
			scene.push_back({ static_cast<int>(v[0]), v[1], v[2], v[3], v[4] });
		}
	}
	std::vector<double> shake = args["shake"].empty() ? std::vector<double>{}
							  : split(args["shake"], ':');
	std::vector<double> pan = args["pan"].empty() ? std::vector<double>{}
						      : split(args["pan"], ':');

	int frames = std::stoi(args["frames"]);
	double noise = std::stod(args["noise"]);
	int latency = std::stoi(args["latency"]);
	int trigger = std::stoi(args["trigger"]);
	int restart = std::stoi(args["restart"]);
	std::mt19937 rng(std::stoul(args["seed"]));
	std::normal_distribution<double> gauss(0.0, 1.0);
	std::uniform_real_distribution<double> uniform(-1.0, 1.0);

	SoftAf af;
	af.setPosition(std::stoi(args["start"]));
	af.setMode(args["mode"] == "auto" ? SoftAf::Mode::Auto
					  : SoftAf::Mode::Continuous);
	af.reset();

	/* What the lens was told, by the frame it was told at. */
	std::vector<int> commanded(frames + 1, std::stoi(args["start"]));
	int current = std::stoi(args["start"]);
	uint32_t streamFrame = 0;

	std::printf("frame,valid,score,command,lens,state\n");
	for (int frame = 0; frame < frames; ++frame, ++streamFrame) {
		if (frame == restart) {
			af.reset();
			streamFrame = 0;
		}
		if (frame == trigger)
			af.trigger();

		int lens = frame >= latency ? commanded[frame - latency]
					    : std::stoi(args["start"]);

		const Segment *seg = &scene.front();
		for (const Segment &s : scene) {
			if (frame >= s.frame)
				seg = &s;
		}
		double d = (lens - seg->peak) / seg->sigma;
		double contrast = seg->base * (1.0 + seg->amp * std::exp(-0.5 * d * d));
		if (pan.size() == 3 && frame >= pan[0] && frame < pan[1])
			contrast = seg->base * (1.0 + pan[2] * uniform(rng));
		if (shake.size() == 3 && frame >= shake[0] && frame < shake[1])
			contrast *= shake[2];
		contrast *= 1.0 + noise * gauss(rng);
		if (contrast < 1.0)
			contrast = 1.0;

		bool valid = streamFrame % 4 == 0;
		std::optional<uint64_t> score;
		if (valid)
			score = static_cast<uint64_t>(contrast);

		int command = -1;
		if (std::optional<int32_t> p = af.process(streamFrame, score)) {
			current = *p;
			command = *p;
		}
		commanded[frame] = current;

		std::printf("%d,%d,%llu,%d,%d,%d\n", frame, valid ? 1 : 0,
			    static_cast<unsigned long long>(score.value_or(0)),
			    command, lens, static_cast<int>(af.state()));
	}
	return 0;
}
