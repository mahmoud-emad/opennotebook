// The slide styles a deck can be built in, as the old app listed them
// (`opennotebook_sdk::styles`). The server's /api/styles is the authority for
// what a build accepts; this copy only draws the picker without a round trip.

export type SlideStyle = { id: string; label: string; blurb: string };

export const STYLES: SlideStyle[] = [
  { id: "editorial", label: "Editorial", blurb: "Magazine type, thin rules" },
  { id: "professional", label: "Professional", blurb: "Report grid, crisp charts" },
  { id: "bento", label: "Bento grid", blurb: "A mosaic of tiles" },
  { id: "instructional", label: "Instructional", blurb: "Numbered steps, arrows" },
  { id: "scientific", label: "Scientific", blurb: "Labelled textbook figures" },
  { id: "sketchnote", label: "Sketch note", blurb: "Doodles on dotted paper" },
  { id: "clay", label: "Clay", blurb: "Soft plasticine shapes" },
  { id: "bricks", label: "Bricks", blurb: "Built from toy bricks" },
];
