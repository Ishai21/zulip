import {$} from "jquery";
import * as z from "zod/mini";

import render_message_recap from "../templates/message_recap.hbs";

import * as channel from "./channel.ts";
import * as dialog_widget from "./dialog_widget.ts";
import {$t} from "./i18n.ts";
import * as loading from "./loading.ts";

const recap_response_schema = z.object({
    sections: z.array(
        z.object({
            heading: z.string(),
            points: z.array(
                z.object({
                    text: z.string(),
                    references: z.array(
                        z.object({
                            message_id: z.number(),
                            url: z.string(),
                            sender: z.string(),
                            conversation: z.string(),
                        }),
                    ),
                }),
            ),
        }),
    ),
    unread_count: z.number(),
    included_count: z.number(),
    truncated: z.boolean(),
});

export function launch_recap_modal(): void {
    dialog_widget.launch({
        modal_title_text: $t({defaultMessage: "Recap of unread messages"}),
        modal_content_html: '<div class="message-recap-loading"></div>',
        modal_submit_button_text: $t({defaultMessage: "Close"}),
        single_footer_button: true,
        close_on_submit: true,
        id: "message-recap-modal",
        footer_minor_text: $t({defaultMessage: "AI recaps may have errors."}),
        on_click() {
            // Just close the modal, there is nothing else to do.
        },
        post_render() {
            const $content = $("#message-recap-modal .modal__content");
            loading.make_indicator($content.find(".message-recap-loading"), {
                text: $t({defaultMessage: "Generating recap…"}),
            });
            const close_on_success = false;
            dialog_widget.submit_api_request(
                channel.get,
                "/json/messages/recap",
                {},
                {
                    success_continuation(response_data) {
                        const data = recap_response_schema.parse(response_data);
                        // Templates cannot test numbers or arrays directly,
                        // so precompute the booleans they branch on.
                        $content.html(
                            render_message_recap({
                                ...data,
                                has_unreads: data.unread_count > 0,
                                has_sections: data.sections.length > 0,
                                sections: data.sections.map((section) => ({
                                    ...section,
                                    points: section.points.map((point) => ({
                                        ...point,
                                        has_references: point.references.length > 0,
                                    })),
                                })),
                            }),
                        );
                    },
                    error_continuation() {
                        $content.empty();
                    },
                },
                close_on_success,
            );
        },
    });
}

export function initialize(): void {
    $("body").on("click", "#recap-button", (e) => {
        e.preventDefault();
        e.stopPropagation();
        launch_recap_modal();
    });

    // Reference links are normal narrow URLs, so the browser's hashchange
    // handling navigates; we just need the modal out of the way.
    $("body").on("click", "#message-recap-modal .message-recap-reference", () => {
        dialog_widget.close();
    });
}
