module Pages.ProductDetail exposing (view)

import Html exposing (Html, div, h1, h2, h3, p, img, span, a, ul, li, text)
import Html.Attributes exposing (class, src, alt, href, style)
import Data.Products exposing (getProductById, Product)

view : Int -> Html msg
view id =
    case getProductById id of
        Nothing ->
            div [ class "card", style "margin" "40px auto", style "max-width" "600px", style "text-align" "center" ]
                [ h1 [] [ text "Product not found" ]
                , p [] [ text "Sorry, we couldn't find the product you are looking for." ]
                ]
        Just product ->
            div [ class "product-detail-container", style "display" "flex", style "flex-direction" "column", style "align-items" "center", style "padding" "32px 0" ]
                [ breadcrumb product
                , div [ style "display" "flex", style "gap" "32px", style "width" "100%", style "max-width" "1000px" ]
                    [ div [ style "flex" "2" ]
                        [ img [ src product.imageUrl, alt product.name, style "width" "100%", style "max-width" "480px", style "border-radius" "12px", style "box-shadow" "0 2px 12px rgba(0,0,0,0.07)", style "margin-bottom" "24px" ] []
                        , h1 [] [ text product.name ]
                        , p [] (List.map text (String.split "\n" product.description))
                        ]
                    , rightWidget product
                    ]
                , reviewSection product
                ]

breadcrumb : Product -> Html msg
breadcrumb product =
    div [ style "margin-bottom" "18px", style "width" "100%", style "max-width" "1000px" ]
        [ a [ href "/", style "color" "#1976d2", style "text-decoration" "none", style "font-weight" "500" ] [ text "Home" ]
        , span [] [ text " / " ]
        , a [ href "/products", style "color" "#1976d2", style "text-decoration" "none", style "font-weight" "500" ] [ text "Products" ]
        , span [] [ text " / " ]
        , span [ style "color" "#222", style "font-weight" "600" ] [ text product.name ]
        ]

rightWidget : Product -> Html msg
rightWidget product =
    div [ class "card", style "flex" "1", style "margin-top" "0", style "margin-left" "24px", style "background" "#f5f7fa" ]
        [ h3 [] [ text "Business Info" ]
        , p [] [ text ("Category: " ++ product.category) ]
        , p [] [ text "Website: ", a [ href product.website, style "color" "#1976d2" ] [ text product.website ] ]
        , p [] [ text ("Phone: " ++ product.phone) ]
        , p [] [ text ("Address: " ++ product.address) ]
        , h3 [ style "margin-top" "18px" ] [ text "Opening Hours" ]
        , ul [] (List.map (\(day, hours) -> li [] [ text (day ++ ": " ++ hours) ]) product.hours)
        ]

reviewSection : Product -> Html msg
reviewSection product =
    div [ class "card", style "width" "100%", style "max-width" "1000px", style "margin-top" "32px" ]
        [ h2 [] [ text "Reviews" ]
        , if List.isEmpty product.reviews then
            p [] [ text "No reviews yet." ]
          else
            ul [] (List.map reviewItem product.reviews)
        ]

reviewItem : { author : String, content : String, rating : Int } -> Html msg
reviewItem review =
    li [ style "margin-bottom" "18px" ]
        [ span [ style "font-weight" "bold" ] [ text review.author ]
        , span [] [ text (" - " ++ String.repeat review.rating "★") ]
        , p [ style "margin" "4px 0 0 0" ] [ text review.content ]
        ] 