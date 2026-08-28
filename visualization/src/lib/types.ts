/** Shared types for the embedding visualization. */

export interface EmbeddingPoint {
	x: number;
	y: number;
	vector_id: string;
	title: string;
	category: string;
	year: number | null;
	resource_id: string | null;
	conference: string;
	text_preview: string;
}

export interface EmbeddingResponse {
	points: EmbeddingPoint[];
	meta: {
		total: number;
		elapsed_seconds: number;
		umap_params: {
			n_neighbors: number;
			min_dist: number;
		};
		categories: Record<string, number>;
		error?: string;
	};
}

export interface TooltipState {
	visible: boolean;
	x: number;
	y: number;
	point: EmbeddingPoint | null;
}

/**
 * Color palette for document categories.
 * Hand-picked for contrast on dark backgrounds.
 */
export const CATEGORY_COLORS: Record<string, string> = {
	tipsheet: '#6c8cff',
	'contest entry': '#ff6b9d',
	audio: '#ffd166',
	dataset: '#06d6a0',
	journal: '#e07cff',
	webinar: '#ff8c42',
	unknown: '#666'
};

export function getCategoryColor(category: string): string {
	return CATEGORY_COLORS[category] ?? CATEGORY_COLORS['unknown'];
}
